# Flush policy: stop refetching the hot set at every capacity flush

Status: design, not built. November 14, 2026, branch `flush-policy-plan` from `perf-real-workload` at `bcc164f`; revised
after independent review (section 7): chunk 0 (counting) accepted with fixes, the first draft's "two halves evicted
whole" withdrawn as the design (it does not protect the hot set). Roadmap item 7 of
[the architecture review](architecture-review-20261113.md) (root cause C), continued by decision 15 of
[io5-perf-notes](io5-perf-notes.md). Evidence: the per-vCPU `vcpu` lines of `natemu-raw/census-profile-1/perf-{configure,make}.log`
(`k1.full`, `k1.peak`, `k1.capacity`, `k1.pic`, `k1.census.answers`, `state`) and the baseline `natemu-raw/chunk1b-profile-1`;
code: `crates/runtime/src/cache.rs` (`place`, `file`, `reset`, `unlink_all`, `cut`, `invalidate`, `block_at`, `template`,
`instance`), `crates/runtime/src/execution.rs` (`find_from`, `flush`, `kept`), `crates/abi/src/execution.rs` (`mod cache`,
`Control`, `Block`), `crates/host/src/execution.rs` (`Answers`, `prepare`, `placed_blocks`, `check_links`),
`crates/driver/src/drivers/program.rs` (`first_link`, `flushing_next`). Numbers marked *estimate* show their arithmetic;
the rest is read from the logs. No code or VM was touched.

## 1. What fills, how often, and what a flush costs

Each vCPU has three bounded pools filled by sequential bump allocation: the code area (128 MiB on IO5; the `b` branch
reach is the ceiling), the metadata arena (256 MiB of records: a 160-byte `Block`, of which 64 bytes are the `links`
array, plus access sites and relocations) and the block index (1 Mi numbers, half of L2 so probes stay short). When any
one is full the whole cache is flushed: every link undone, L1, L2, chains and the three fill points reset, and every
block fetched again from the host's answer cache.

Peaks before a reset, per vCPU (census profile; `k1.peak` against `k1.capacity`):

| | configure (4 vCPUs) | make -j4 (4 vCPUs) |
| --- | --- | --- |
| code bytes at the flush | 75-79 MB of 134 MB (**56-59%**) | 103-111 MB (**77-83%**) |
| record bytes at the flush | 223-226 MB of 268 MB (**83-84%**) | 244-251 MB (**91-93%**) |
| block numbers at the flush | 1,048,576 of 1,048,576 (**100%**, all vCPUs) | 1,048,576 (**100%**) |
| which pool was full (`k1.full`: code / records / numbers) | 0 / 0 / 11 | 0 / 0 / 10 |
| flushes (of them for capacity) | 15 (11); 3-4 per vCPU | 14 (10) |
| time inside the flush itself (`flush_us`) | about 27 ms each | about 50 ms each |

**The block index fills first, on every vCPU, on both steps.** On configure the records are at 84% and the code at 58%
when it does; on make the records are at 93% and the code at 80%, so there all three pools are within about 20% of each
other.

What the numbers hold (sums over the four vCPUs, `capacity_flushes` x 1 Mi + `used[2]`):

| filed in the index | configure | make -j4 |
| --- | --- | --- |
| total | 13.55 M | 12.55 M |
| PIC instances (`pic[INSTANCES]`, the perf log's "reuse copies": a 160-byte record, no code, one per process per shared-library block under ASLR) | 9.19 M (**68%**; 66-69% per vCPU) | 5.37 M (**43%**; 37-45%) |
| host-placed blocks (`state.requests`) | 3.32 M (25%) | 6.30 M (50%) |
| shared translations (`pic[SHARES]`) | 0.67 M (5%) | 0.69 M (5.5%) |
| system-instruction records and the rest | 0.37 M (3%) | 0.19 M (2%) |

So on configure two thirds of the numbers hold no code (72 bytes of code and 213 of record per filed block); on make under
half (101 and 236 bytes). Instances are per process: they are the cold churn no retention policy keeps; the hot set is
the kernel, the running compiler and the libraries' translations.

What the flush throws away (decision 15's counter: a host hit for an answer this vCPU had placed before its last flush):
**1.155 M of configure's 3.11 M host hits (37.1%) and 2.83 M of make's 5.44 M (52.0%)**: about 100 k refetches per
capacity flush on configure and 280 k on make. Each is a VM exit, lookup and admission on the host, the reply, placement,
then the block's links, its L1 entry and, for a template, its shared translation and instances again.

Cost per refetch (*estimate*): host hit 2.9 µs on configure and 3.4 µs on make (the cross-vCPU plan's figure, timing log
excluded) plus in-VM about 1.3 µs (placement 0.2, an L1 miss 0.3, about 1.5 links at 0.5) = **4.2 and 4.7 µs**. Upper
bound of what any flush policy can recover: configure 1.155 M x 4.2 µs = **4.9 vCPU-s (4.1% of 119.8)**, make 2.83 M x
4.7 µs = **13.3 vCPU-s (2.2% of 603)**; about 0.4 s and 1.3 s per capacity flush. The flush's own work (27-50 ms) is
not the cost; the refetching after it is. With the shared pool in place the host part of a refetch falls to about
0.75 µs, so the recoverable part per refetch becomes about 2.0 / 2.5 µs (section 4).

## 2. What a policy can and cannot do (the review's correction)

A **hot block** (one entered every few thousand placements: kernel entry paths, the compiler's own loops, libc) is lost at
every flush and refetched at once. Its refetch rate is one per cache cycle, C placements, whatever the order in which the
cache is emptied: evicting the oldest half instead of everything moves the block into the newly emptied half, where it
lives exactly one more cycle. Halves (and any FIFO over regions) only help blocks whose next use comes more than about
C/2 placements after their loss, and they add events, cut the instances of evicted translations and lose hints. The first
draft's "mean live set 3C/4, capacity x 1.5" was true of the pool as a whole and false of the hot set, which is where
the refetches are. **The lever is to carry the hot set across the flush**, or to make cycles longer; not to shuffle
which blocks die first.

What no policy fixes: the per-process instances (68% of configure's numbers) are placed once per process and die with
it; they set the cycle length. Making them smaller or not charging them an index number lengthens the cycle; that is
candidate (a).

## 3. Three candidates, decided by chunk 0

**(a) Longer cycles: slim instance records, double the index and L2.** An instance's `links` (64 of its 160 bytes) is its
translation's `links` with every target moved by `rip - t.rip` (`instance`, `cache.rs`); the translation is live as long as
the instance is (both die with a write to their page). So the array can be derived on read (`links_of(b)`: the
translation's, moved, when `INSTANCE`), and the record shrinks to about 96 bytes: a `Block` without `links`, with the
array as a tail (like the sites) for blocks that have one. Then double `MAX_BLOCKS` to 2 Mi and `L2_ENTRIES` to 4 Mi
(+32 MiB of L2 and +4 MiB of index per vCPU in the lookup window; its room must be checked against `LOOKUP_BYTES`). With
the index no longer first, the records fill at (*estimate*) configure 268 MB / (213 - 0.68 x 64 = 169 bytes) = 1.59 Mi
blocks per cycle (x 1.5), make 268 / (236 - 0.43 x 64 = 208) = 1.29 Mi (x 1.25; code reaches 130 of 134 MB, so the code
area is next). On configure too the code area is the next limit almost immediately: at 1.59 Mi blocks the code is at
about 115-120 MB of 134 (85-90%; the reviewer puts it near 95%), so (a) cannot be stretched past about x 1.5-1.7 by
any record saving, and chunk 0's replay uses the real per-block code sizes to find which pool fills first. Refetches
scale with flushes for the cold-returning blocks and stay one per cycle for the hot set: about **-30% on configure,
-17 to -20% on make** (the reviewer's figures agree), and like every replay of a policy that keeps more, this one is
optimistic (hits (a) would have missed are not in today's trace). No eviction machinery; the new correctness surface is
the record accessor and its readers: `Block::rep_exit` and `sites`/`relocs` (the sites' offset moves once `links` is a
tail; order the tail as links, sites, relocations), the resident's `REUSE_CHECK` comparison (`checked` in `execution.rs`,
which compares a copy's record with the host's answer), the linker (`svc_slot`, `link`), `instance`, `share`,
`links_restored`, the host's `check_links` and the driver's `first_link`. Memory: L2 is hashed, so +32 MiB of resident
memory per vCPU; the arena's RSS is unchanged.

**(b) Flush, but carry the hot blocks forward.** At a capacity flush the hot live blocks are copied down to the bottom of
the pools with new numbers; everything else is dropped as today. Order inside `flush`:

1. `unlink_all` and `links_restored` (as today): no link exists, every link point holds its admitted word.
2. Collect the carried set (in increasing number order) and the set of its source pages. Hot means `checked >= epoch_mark`,
   the epoch when the fill passed the mark chunk 0 chooses (the last quarter of the cycle as the default), live, with
   code or a `SYSTEM` record; a `PIC`-shared translation only if an instance of it is carried (one pass over the instances
   first); capped at a quarter of the capacity so an all-hot pathology still makes progress.
3. `reset`'s clearing: fill points, L1 tables and their contexts, dispatcher slots, L2, chain heads, linear heads, regions,
   and the code-page bits, with one change: the clear spares the carried pages (per mask word, clear this vCPU's bits minus
   those pages' bits, one atomic each), so there is no window in which a peer's write to a carried page would not post.
4. **Reserve a dispatcher slot per carried context** before any exit is relocated: after a reset only the current context
   has a slot, so carried blocks of other contexts (hot kernel blocks when the flush happens in user context) would
   otherwise exit to the miss path for the whole cycle. For each distinct context among the carried blocks:
   `slot_contexts[k] = context`, `write_slot(k, None)` (a `b` to the miss until a table binds); `bind_slot` later finds the
   slot by context.
5. Carry, in increasing number order: the code bytes moved to the new fill point (each destination is at or below its
   source because the order is increasing and only a subset is kept; `copy_record` copies forward, ascending, so an
   overlapping move is sound in that order), the computed exit relocated through `dispatcher_for(context)` after the
   reset (it names the slot reserved in step 4), the code synchronized at its new address, then the record re-filed by the
   ordinary `file` (new number; sites and relocations copied; L2, both chains, regions and the hint of a `RELOCATABLE`
   block written by `file` itself). Translations first, then instances with `shared` rewritten through a small old-to-new
   number map. A carried translation keeps the PIC layout `share` made: the 16 lead bytes it wrote at `code - 16` move
   with it (the full entry is at `code - 4`), the new code sits `PIC_LEAD` past a 64-byte boundary as `share` places it,
   and the synchronization covers lead and code.
6. `flushes += 1`, both generations bumped (as `flush` does today), `epoch_mark` reset for the new cycle.

The hot set then survives every flush; cold-returning blocks are as today.

**(c) Two halves plus carry-over.** The first draft's halves (fill the pools as two halves, evict the older one whole,
rebuild the survivors' structures) with (b)'s carry-over at each eviction. Keeps the cold-returning blocks of the newer
half too, at the price of two eviction events per cycle (each 50-80 ms, *estimate*: `unlink_all`, a 32 MiB L2 memset,
a 500 k-record survivor pass), of the cascade of instances whose translation sits in the older half, of a second set of
fill points for every reader of `Control` (host `placed_blocks`, `check_links`, driver `first_link`, `flushing_next`),
and of the 32 KiB test code areas (`--code-cache 32768`) that cannot hold two maximal blocks. Section 7 lists what it
must also get right (B1, B2). Kept as a candidate; built only if chunk 0 shows the cold-returning share pays for it.

**Rejected outright.** Larger areas across the board: the code area cannot grow (128 MiB is the `b` reach), the records
have exactly 256 MiB of address space below the lookup area; a real doubling is a new layout and about 300 MB more per
vCPU at peak for one more cycle length, which (a) buys for 32 MiB. Compaction of everything: renumbers every record, for
a gain (b) gets by moving only the hot set. Per-block LRU or many regions: a use signal on the dispatcher's hot path and
predecessor-less chains. Evict by currency: currency is per address space; the next process is never current at the
flush. The pool alone: it lowers the price of a refetch, not their number; 20% of hits are not templates and the
placement, links, L1, translation and instances are paid again regardless.

## 4. Gain and cost (*estimates*; chunk 0 replaces the unknowns)

Unknowns chunk 0 measures: the share of refetches that are the hot set under a carry window (h), the share whose gap
falls between C/2 and C (g, what halves add), and the hot set's size at a flush (the carry cost).

| per step, vCPU-s | configure (serial: about wall seconds) | make -j4 |
| --- | --- | --- |
| upper bound, host paths as today / with the pool | 4.9 / 2.3 | 13.3 / 7.1 |
| (a) at -30% / -18% of refetches; no event cost | 1.5 / 0.7 | 2.4 / 1.3 |
| (b) at h = 0.5-0.8, minus carry (100 k blocks x 0.3 µs x 11 flushes = 0.33 s; make 0.5) | 2.1-3.6 / 0.8-1.5 | 6.1-10.1 / 3.0-5.2 |
| (c) = (b) + g x upper bound, minus one more eviction per cycle (11 x 50-80 ms = 0.6-0.9 s; make 0.5-0.8) | (b) - 0.6-0.9 + g x 4.9 | (b) - 0.5-0.8 + g x 13.3 |

Reading: with the pool, (c) on configure nets about zero over (b) unless g is above 0.15-0.2; (a) is a fifth to a half
of (b) but has no event cost and the least machinery. Overlap with the cross-vCPU pool: the host part of each avoided
refetch, roughly half of every figure; the "with the pool" columns are the ones to judge once the pool ships. (a) and
(b) compose (slimmer records lengthen the cycle; carry-over keeps the hot set across it); (a) and (c) compose the same way.

## 5. Chunks, gates, tests

**Chunk 0, counting and replay (scratch in `natemu-raw/io5-bins/`, not committed; one profile of configure and make,
scheduled by the main session).**
- Host trace: every translation request as `(vCPU, answer fingerprint, the answer's first frame, RIP, context and
  relocatable flag, hit-or-compile, and the vCPU's `Control::blocks`, `code_used`, `meta_used`, instances counter
  (`pic[INSTANCES]`) and `generation` at the request)`, about 48 bytes x 1.7 M per vCPU, beside the timing log (the host
  maps the lookup area; each field is one load). All three fill points are needed so the replay knows when the records or
  the code would fill under another record size or index, and the instances counter so (a)'s replay can recharge
  instances at 96 bytes; instances and shares between two requests also show as the gaps.
- Resident, at each capacity flush: for every live block with `checked` at or above each of four marks (the epochs when the
  fill passed 1/2, 3/4, 7/8 and 15/16 of capacity), its identity (first frame, RIP, context) and flags, written to a
  scratch page the host drains with the trace; plus the counts per mark and the live instances per mark. This is what
  carry-over would have kept at each window. The refetch trace is matched against it by two keys: an absolute block by
  (first frame, RIP, context); a `RELOCATABLE` template or a translation by the **hint key** (first frame, RIP's page
  offset and address half, context), because a carried template serves a later request at another RIP of the same frame
  through the hint table (`template`), and those are configure's dominant shared-library refetches: matching them by
  full RIP would undercount h.
- Readouts: (1) a histogram of refetch delay since the flush, in placements (halves can only remove delay above about
  C/2: that is g); (2) for each window, the share of refetches whose block was in the window's set at the flush (h) and the
  set's size and bytes (the carry cost); (3) the replay of (a): the same trace with instances charged 96 bytes, the index
  at 2 Mi, and the code and record fill points advanced by the traced per-request deltas, flushing when the first pool
  fills (validated by first reproducing today's 1.155 M / 2.83 M within a few percent); (4) spurious posts: pages posted
  to a vCPU whose chain was empty (for the code-page-bit rule). Both the halves replay and (a)'s are **optimistic**: a
  policy that keeps more would have hit in-VM on requests the trace does not hold (the halves also cut instances and lose
  hints); state that beside each number.
- **Decision rule:** net modelled gain per candidate with the pool's per-refetch cost, the event cost of section 4 and
  the measured h, g and set sizes; **prefer (a) if it is within about 70% of the best**; otherwise (b), or (c) only if
  g's contribution exceeds its extra eviction cost by a clear margin on make. If no candidate nets at least 1% of busy on
  either step with the pool, stop and record (counters stay).

**Chunk 1, the chosen candidate.** Common to all: `capacity_flushes` and the refetch counter (decision 15's, made
permanent: it is the gate's reading), per-vCPU `peak`/`used`/`capacity` in the perf line and `tools/io5_breakdown.py`.

- *(a)*: the `Block` accessor refactor (`links_of`), the tail layout, `MAX_BLOCKS`/`L2_ENTRIES` doubled with the
  `LOOKUP_BYTES` check, host `check_links` and the driver's readers through the accessor. Tier 0: layout unit tests
  (record sizes, the tail's alignment, L2 load at most a half, the lookup window's room); Tier 1: K1 1 and 4 vCPUs
  (the `reuse-*` and `pic-*` bodies exercise instances' links). Mutation: `links_of` returning the translation's targets
  unmoved (red on `pic-twosrc`, `pic-xwrite`). Tier 2 gate: capacity flushes down by a third on configure and a fifth on
  make, refetches down accordingly, `host` down by at least 70% of (a)'s low estimate, no bucket up by half the gain.
- *(b)*: `carry()` inside `flush`, as section 3 says; `Control` gains `epoch_mark`, `carried`, `carry_ticks`; `flush`
  keeps bumping both generations (`shadow_generation` too: carried code reuses native addresses, and the no-progress
  check compares retries by PC and shadow generation). The resident's `flushed` flag in `find_from` is cleared after a
  successful placement (a search that places more than once after a flush must not halt `BAD_REPLY` on a later full).
  Code-page bits: `reset`'s `clear_own` is replaced by a clear that spares the carried blocks' pages (per mask word, clear
  own bits minus those pages' bits, one atomic each), so no window exists in which a peer's write to a carried page would
  not post (a stale bit is not kept: a peer's kick costs about 125 µs on configure). `pic-count` builds: the computed
  stubs naming a dropped block are reset with the dispatcher slots. Tests (K1, natemu against QEMU, 1 and 4 vCPUs,
  `--code-cache 32768` as the suites use, plus a **host-set cap on numbers** (`Control::max_blocks`, test only, like the
  link cap) so a case fills the index with a few hundred instances as production does; `stats.flushes` and `carried`
  asserted):
  - `carry-hot`: a loop block entered every round across three flushes: the host answers it once (`requests` for it = 1);
  - `carry-write`: after a flush, the guest rewrites the page of a carried block and the page of a dropped-but-not-yet-
    overwritten one, then runs both: new bytes' effects;
  - `carry-translation`: write a carried translation's page after the flush, then instance it at a new address: new
    bytes (B1's case: translations must be on their source chains);
  - `carry-instance`: carried instances of a carried translation (`shared` remapped) and of a dropped one (cut); the index
    is refilled past the translation's old number before the instances run again;
  - `carry-link`: calls between carried and dropped blocks, taken again after the flush, under `link-check`;
  - `carry-peer` (4 vCPUs, the `pic-xwrite` handshake): a peer rewrites a carried block's page right after the flush.
  Mutations, each red: L2 left as it was (not zeroed: `carry-write`, the dropped page); translations left off their
  source chains (`carry-translation`); L1 not cleared (`carry-write`: the stale entry enters old code); `links_restored`
  checking only the carried range (`carry-link` under `link-check`); `shared` not remapped (`carry-instance`); the
  code-page clear not sparing carried pages (`carry-peer`); `shadow_generation` not bumped (red on `carry-retry`: two
  guest blocks of the same native shape, each storing to a page not yet shadowed at the same native offset; the first,
  placed at native PC E, is retried at F after its abort; the number cap then forces a flush with nothing carried, so the
  second block is placed at the same E and its store is retried at the same F: the no-progress check sees the same
  (PC, shadow generation) twice and halts `NO_PROGRESS` without the bump; values against QEMU with it); slots not
  reserved for carried contexts (`carry-hot` in a kernel/user mix: the host answers the hot block once but the `miss`
  count for its exit stays high, asserted); carry writing the record before relocating the exit (`carry-hot`).
  Tier 2 gate: refetches down by h within 10 points; `host` down by at least 70% of (b)'s low estimate; `carry_us`
  under 0.4 s per vCPU per step; `miss` and `link` not up by more than half the gain.
- *(c)*: (b)'s list plus the halves of the first draft with the review's fixes (section 7): every non-dead survivor back on
  its source chains as `file` chains it; both generations bumped; fill points kept absolute (the host's and driver's
  readers unchanged); hints rewritten for `RELOCATABLE` survivors; a half that cannot hold a maximal block (code areas
  under 64 KiB + head) falls back to whole flushes rather than raising `prepare`'s minimum; `evicted_clean` under
  `link-check` (no L2 entry, chain link, L1 owner or record names the evicted half). Its K1 cases are (b)'s with an
  eviction in place of the flush, and `evict-instance` refilled past the translation's number before re-running.

**Chunk 2, measurement.** Group H of the review with the pool: base / pool / pool + this, three interleaved rounds, wall
judged on the group; then the standing suite once before merge and the benchmark round at completion.

**Chunk 3 (only if (a) was chosen and the refetch share after it is still above a quarter of host hits):** (b) on top of
(a). They compose; chunk 0's h decides whether it is worth it then.

## 6. Stop rule and risks

- Chunk 0 decides; nothing is built before its readouts. No candidate netting 1% of busy with the pool -> stop, record.
- Chunk 1: the Tier 2 gate missed after one round of diagnosis -> revert, keep the counters, record why. Any `BAD_LINK`,
  `link-check` halt, `NO_PROGRESS`, `evicted_clean` failure or shadow-mode disagreement traced to the design stops the
  item for re-review.
- Risks: (1) carry-over's window is a heuristic; too wide carries cold blocks (cost), too narrow misses hot ones (gain);
  chunk 0's four marks bound it, and the quarter cap bounds the cost. (2) The replay's transfer assumption (placements
  between requests independent of the policy) is approximate, and the halves replay is optimistic; the Tier 2 counter is
  the acceptance reading. (3) (a)'s accessor touches every reader of `links`, host and driver included; the mutation and
  `check_links` cover it. (4) Instances remain two thirds of configure's numbers; a later lever (instances without an
  index number; the pool making sharing unnecessary) is noted, not planned. (5) The relink plan's suspended records, if
  built, are dropped at a flush or eviction like all records.

## 7. Review response (independent review of `b21b1ff`, November 14)

Accepted in full. What changed:

- **B1 (blocking, halves).** The draft's rebuild put only non-translation survivors on the source-page chains, but `file`
  chains every record; a translation left off its chain survives a write to its frame, `template` returns it and
  `instance` runs old code. Fixed: every non-dead survivor is re-chained as `file` chains it (in (b), `file` itself does
  the filing); K1 case `carry-translation` / its `evict-` twin, and a mutation "translations off source chains".
- **B2 (blocking, halves).** An eviction reuses native code addresses and must bump `shadow_generation` as `flush` does,
  or the no-progress check halts when a new block at a reused PC repeats the last retry. Fixed in (b) (inside `flush`)
  and required of (c); mutation added.
- **Design.** Halves evicted whole do not protect the hot set (section 2); the "3C/4" claim is withdrawn; the
  instance cascade and lost hints are counted against (c). The plan is now three candidates decided by chunk 0, with
  carry-over evaluated before anything is built (the draft's gates, which could have stopped the work on the halves'
  number alone, are gone). Candidate (a) added as the reviewer proposed, with its record arithmetic checked against
  `instance`: the `links` array is derivable.
- **Numbers.** Instances are 68% of configure's numbers and 43% of make's (sections 1, 4 and risk 4 corrected); the
  gain table now carries each candidate's own event cost and the with-pool column (halves on configure net about zero).
- **Carried fixes** for whichever design survives: hints rewritten for `RELOCATABLE` survivors (`file` writes them);
  `flushed` cleared after a successful placement; `pic-count` computed stubs reset; host `placed_blocks`/`check_links` and
  driver `first_link`/`flushing_next` read absolute fill points, which stay absolute; `prepare`'s 32 KiB minimum
  untouched; code-page bits cleared exactly, sparing carried pages, instead of kept as a superset.
- **Tests.** A host-set cap on numbers so cases fill the index with instances; `evict-instance`/`carry-instance` refill
  past the translation's number before re-running; the "L2 not rebuilt" mutation is "L2 left as it was"; mutations for
  L1 not cleared, translations off source chains, `links_restored` over one range; every case at the suites'
  `--code-cache 32768` with the number cap, seconds each.

**Second round (re-review of `2afdb72`; B1 and B2 closed, accepted with remaining issues, all taken):**
- Chunk 0 trace now carries `code_used`, `meta_used` and the instances counter per request, so the replay can tell which
  pool fills and recharge instances at 96 bytes; h matches relocatable templates and translations by the hint key, not
  the full RIP; (a)'s replay is marked optimistic like the halves'.
- (b) states the order inside `flush` (unlink, collect, reset with the sparing clear, reserve slots, carry with exits
  relocated through `dispatcher_for` after the reset, bump both generations), the forward direction of `copy_record` the
  in-place move relies on, the dispatcher-slot reservation per carried context, and the PIC layout a carried translation
  keeps (16 lead bytes, full entry at `code - 4`, `PIC_LEAD` past a 64-byte boundary, synchronization of both).
- (a) lists its readers to convert and notes that on configure the code area is at 85-90% at 1.59 Mi blocks, so it is the
  next limit almost immediately and (a) cannot be stretched further.
- The `shadow_generation` mutation has a concrete red case (`carry-retry`).

## Questions for the owner

1. Approve chunk 0 (one profile run with the trace and per-flush identity log) and its decision rule, with (a) preferred
   when within about 70% of the best?
2. For (a): +32 MiB of resident memory per vCPU (L2 doubled) is acceptable?

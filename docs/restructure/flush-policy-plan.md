# Flush policy: evict half the cache instead of all of it

Status: design, not built, November 14, 2026, branch `flush-policy-plan` from `perf-real-workload` at `bcc164f`. Roadmap
item 7 of [the architecture review](architecture-review-20261113.md) (root cause C), continued by decision 15 of
[io5-perf-notes](io5-perf-notes.md). Evidence: the per-vCPU `vcpu` lines of `natemu-raw/census-profile-1/perf-{configure,make}.log`
(fields `k1.full`, `k1.peak`, `k1.capacity`, `k1.census.answers`, `state`, `pic`) and the baseline
`natemu-raw/chunk1b-profile-1`; code: `crates/runtime/src/cache.rs` (`place`, `file`, `reset`, `unlink_all`, `cut`,
`invalidate`, `block_at`, `template`), `crates/runtime/src/execution.rs` (`flush`, `kept`, the block search),
`crates/abi/src/execution.rs` (`mod cache`: area sizes, `Control`, `Block`), `crates/host/src/execution.rs` (`Answers`,
`prepare`). Every number marked *estimate* shows its arithmetic; the rest is read from the logs. No code or VM was touched.

## 1. What fills, how often, and what a flush costs

Each vCPU has three bounded pools filled by sequential bump allocation: the code area (128 MiB on IO5, the `b` branch range
is the ceiling), the metadata arena (256 MiB of records: a 160-byte `Block` plus its access sites and relocations) and the
block index (1 Mi numbers, half of L2 so probes stay short). When any one is full the whole cache is flushed: every link
undone, L1, L2, chains and the three pointers reset, and every block fetched again from the host's answer cache.

Peaks before a reset, per vCPU (census profile; `k1.peak` against `k1.capacity`):

| | configure (4 vCPUs) | make -j4 (4 vCPUs) |
| --- | --- | --- |
| code bytes at the flush | 75-79 MB of 134 MB (**56-59%**) | 103-111 MB (**77-83%**) |
| record bytes at the flush | 223-226 MB of 268 MB (**83-84%**) | 244-251 MB (**91-93%**) |
| block numbers at the flush | 1,048,576 of 1,048,576 (**100%**, all vCPUs) | 1,048,576 (**100%**) |
| which pool was full (`k1.full`, code / records / numbers) | 0 / 0 / 11 | 0 / 0 / 10 |
| flushes (of them for capacity) | 15 (11); 3-4 per vCPU | 14 (10) |
| time inside the flush itself (`flush_us`) | about 27 ms each | about 50 ms each |

**The block index fills first, on every vCPU, on both steps.** On configure the records are at 84% and the code at 58%
when it does; on make the records are at 93% and the code at 80%, so all three pools are within about 20% of each other.

Why the index and not the code: the fills are mostly records without code. Per vCPU on configure (vCPU 0): about 3.32 M
blocks filed over the step (3 x 1,048,576 + 174,692 left at the end), of which **2.20 M are PIC instances** (`pic[INSTANCES]`,
the same figure the perf log calls reuse copies: a record of 160 bytes, no code, one per process per shared library block
under ASLR), 0.87 M are host-placed blocks (`state.requests`), 0.16 M are shared translations and about 90 k are
system-instruction records. So two thirds of the numbers hold no code, which is why the code area is the slackest pool
(72 bytes of code per filed block on configure, 101 on make) and the records the second (213 and 236 bytes per block).

What the flush throws away (decision 15's counter: a host hit for an answer this vCPU had placed before its last flush):
**1.155 M of configure's 3.11 M host hits (37.1%) and 2.83 M of make's 5.44 M (52.0%)**: about 100 k refetches per
capacity flush on configure and 280 k on make. Each refetch is a VM exit, a lookup and admission on the host, the reply,
placement, and then the block's links, L1 entry and (for a template) its shared translation and instances again.

Cost per refetch (*estimate*): host hit 2.9 µs on configure and 3.4 µs on make (the cross-vCPU plan's figure, timing log
excluded) plus in-VM about 1.3 µs (placement 0.2, an L1 miss 0.3, about 1.5 links at 0.5) = **4.2 and 4.7 µs**. Upper
bound of what the flush policy can recover: configure 1.155 M x 4.2 µs = **4.9 vCPU-s (4.1% of 119.8)**, make 2.83 M x
4.7 µs = **13.3 vCPU-s (2.2% of 603)**; about 0.4 s and 1.3 s per capacity flush. The flush's own work (27-50 ms) is
not the cost; the refetching after it is.

## 2. Options weighed

**Larger areas.** The code area cannot grow: 128 MiB is the reach of the `b` that links and the dispatcher use
(`CODE_BYTES_MAX`); the records have exactly 256 MiB of address space below the lookup area; the index needs L2 to double
with it (32 -> 64 MiB in the lookup window, plus 4 MiB of index). Doubling the index alone, the review's "1 -> 2 Mi", buys
make about 7% more blocks before the records fill (93% -> 100%) and configure about 20%: not a policy, a delay. A real
doubling of all three means a new area layout and about 300 MB more resident memory per vCPU at peak (1.2 GB for four),
for a mean live set of C instead of C/2 (section 3 explains the mean). Kept as a later knob that composes with the design
below, not as the design.

**Partial eviction by halves (chosen).** Treat each pool as two halves filled in turn; when the half being filled is full,
evict the other half whole and fill it. The mean live set rises from C/2 to 3C/4 with no memory added, each eviction is a
bounded pass over the survivors, and no record is renumbered or moved. Section 3.

**Keeping hot blocks across an eviction.** Copy the recently used blocks of the half about to be evicted into the live half
(as `copy` places a template at another address: code, relocated computed exit, record with a new number). The signal is
`Block::checked`: a block entered since the live half began filling was revalidated at or after that epoch. Worth doing
only if many such blocks exist in the old half at eviction time; the counting chunk measures that first. Section 3.4.

**Compaction / renumbering.** Moving survivors down renumbers them, and numbers live in L2, both chains, the hint table, the
PC hints, the link records, L1's owners and instances' `shared` field. Rejected: every one of those is a correctness
surface, for a gain the halves already get (the half boundary is a free compaction).

**Per-block eviction (true LRU, or many small regions).** Needs a use signal written on the dispatcher's hot path, and
removal from singly linked chains needs the predecessor. Rejected; two halves with a rebuild of the survivors' structures is
the simplest exact scheme, and four or more regions multiply the rebuild cost per cycle.

**Evict by currency (keep only current blocks).** Currency is per address space: at the instant configure flushes, the
next process's `cc1` is not current and the previous one's is dead. Rejected as the eviction criterion; recency of
`checked` is the carry-over heuristic instead.

**Leaning on the shared pool alone.** The pool serves a refetch in about 0.5-1.0 µs instead of 2.9-3.4 µs, but placement,
links, L1, the shared translation and its instances are paid again regardless (about 1.3 µs), and about 20% of hits are
not templates. The pool lowers the price of a refetch; this plan lowers their number. Both should exist; section 4
states the overlap.

## 3. Design: two halves

### 3.1 Layout and placement

Each pool is split at its midpoint: code `[HEAD_BYTES, mid)` and `[mid, code_bytes)` (mid 64-byte aligned), records
`[0, 128 MiB)` and `[128, 256 MiB)`, numbers `[0, 512 Ki)` and `[512 Ki, 1 Mi)`. `Control` gains the half being filled
(`half`) and the other half's three fill points (the current half keeps using `code_used`, `meta_used`, `blocks`, now
relative to its half's start), plus `epoch_half` (the epoch when the current half began filling), `evictions` and
`evict_ticks`. Every placement path (`place`, `copy`, `share`, `instance`, `place_system`) already tests the three fill
points against a limit; the limit becomes the half's end, in one helper. A half holds any block: `prepare` requires a
code area of at least 2 x `MAX_BLOCK_BYTES` (64 KiB; tests use that), and half the arena holds the largest record.

The block search in `execution.rs` keeps its shape: placement fails -> `evict()` -> place again -> if it fails again,
`BAD_REPLY` as today. The capacity flush is no longer called there; `flush` stays for invalidation requests
(`FLUSH` pending bit) and new instruction breakpoints, and keeps resetting both halves.

Block numbers no longer form one dense range. A number `n` is valid when it lies below its half's fill point; `template`
(the hint table's check, today `n >= blocks`), `block_at` (one binary search per half; the half is known from the PC) and
`links_restored` (both ranges) use that test. Index entries and records of unfilled slots are never read.

### 3.2 Eviction of half H (the half not being filled)

Runs where a capacity flush runs today: in the block search, no generated code live, state banked, any armed stop
restored. Steps, each reusing an existing routine where one exists:

1. **Links.** `unlink_all` and `links_restored`, exactly as `flush`: every record is dropped and every link point holds its
   admitted word again. Links are remade lazily by the linker. (Dropping only records touching H is possible through the
   inbound lists and one pass over the records, but links are undone and remade thousands of times per CR3 load already;
   the simple form costs at most the survivors' live links, about 100 k x 0.5 µs = 50 ms per eviction, and is not worth
   a second code path until measured.)
2. **Instances of evicted translations.** A survivor with `INSTANCE` whose `shared` names a number in H has no code left:
   it is marked `DEAD` and left off every structure rebuilt below (the survivor scan of step 5 finds them).
3. **L1.** All four tables cleared (`clear_l1`), their contexts and dispatcher slots kept: survivors still exit to their
   context's slot and refill L1 by misses (the hot set is a few thousand entries at 0.3 µs).
4. **L2, chains, regions.** L2 zeroed; the reverse map's heads for pages in this vCPU's code summary zeroed and the linear
   heads zeroed (as `reset` does); `regions`, `large_regions`, `large_list`, `large_count` cleared.
5. **Rebuild from the survivors**, one pass over the other half's numbers in order: for each live block that `file` would
   have entered (not `DEAD`, not a `TRANSLATION`), insert it in L2 by (RIP, context), push it on its source pages' chains
   and its linear pages' chains (rewriting `next` and `lnext`), and mark its regions (`mark_regions`, `mark_large` for
   `LARGE`). Dead survivors stay dead and off the chains, as they are after death today.
6. **Pointers.** H's three fill points reset; H becomes the half being filled; `epoch_half = epoch`; `evictions += 1`;
   the resident `State::generation` is bumped as a flush bumps it (every path that compares generations, `kept`, the
   linker, `after_system`, the step path, then re-looks its block up; the surviving ones are found again at once).
   `shadow_generation` does not move: no mark changed.

What is deliberately not touched: this vCPU's **code-page bits** in the global mask stay set for pages whose last
translation was evicted. They mean "may hold translations"; a stale bit only makes a peer post a page this vCPU drains
with an empty chain (`invalidate` then clears the bit). Clearing them exactly would need a scan for pages with no
survivor, and the window between deciding and clearing is safe only if argued; the count in chunk 0 says whether the
spurious posts matter. `pc_hints` and the hint table are candidates checked against the record they name (`block_at`
checks the code range, `template` the frame, context and RIP half and offset), so a number reused by a later block
either fails the check or is the right block.

### 3.3 Correctness argument

- **Links.** After step 1 no record exists and every link point holds its admitted word (checked by `links_restored`),
  so no survivor branches into H's code and no code in H is a link target; this is the state a flush leaves. The
  `link-check` resident's invariants hold trivially.
- **L1.** Cleared; no entry names a number in H. An L1 entry is filled only from `lookup`, which reads L2.
- **L2.** Rebuilt from survivors only; a probe cannot reach a number in H. `lookup`'s contract (a block is returned only if
  live, keyed by RIP and context, current or revalidated by a walk) is unchanged because the entries are the same ones
  `file` wrote, minus H's.
- **Invalidation (SMC, peers).** A peer's write to page P posts P to every vCPU with a code bit for P; this vCPU's bits
  are a superset of its survivors' pages (never cleared at eviction), so every survivor's page still posts. `invalidate(P)`
  walks P's rebuilt chain, which holds exactly the live survivors from P. The chain never passes through H, so refilling
  H's records cannot corrupt a walk. A write to a page whose only blocks were evicted finds an empty chain. INVLPG and #PF
  use the regions and linear chains, rebuilt from the same survivors.
- **In-flight execution on this vCPU.** No generated code is live at the eviction point (as at a flush); the interrupted
  block exited into the resident before the search began, and the search continues with a lookup after the eviction, never
  a return into H. The generation bump makes any block reference held across the eviction (the request's `missed`
  generation, the linker's source, a system instruction's `ran`) re-looked up, as after a flush.
- **Other vCPUs.** They hold no number of this vCPU; they only post pages and read its code-page and writable bits. Neither
  is changed by an eviction. The split lock and shadow protocol are untouched.
- **Epochs and currency.** `epoch`, `global_epoch` and every survivor's `checked` are unchanged; `current` reads the same
  fields. A survivor that was current stays current; one that was not is revalidated by a walk as before. The host's
  answer cache is content-keyed and knows nothing of the resident's halves.
- **Instances and translations.** A translation's code lives in its own half; an instance runs `code_of` = its
  translation. Step 2 removes every instance whose translation is evicted; a translation whose instances are evicted
  stays and is instanced again when needed (it is found through the hint table, checked by content).
- **Reused memory.** Records and code of H are overwritten only after steps 1-6, when nothing names a number in H except
  the lazily checked hints, which validate by content.

### 3.4 Hot carry-over (chunk 3, gated)

At eviction, before step 6, blocks of H with `checked >= epoch_half` (entered since the live half began) that are live,
have code and are not `PIC`-shared translations are copied into the live half as `copy` places a template: code bytes,
the computed exit relocated to the context's slot, a record with a new number, sites and relocations copied, then
filed by the normal `file` so it lands in L2 and the chains directly. A translation is carried if one of its hot instances
is carried (one pass over H's instances first). Carry-over is capped at a quarter of a half so an all-hot pathology still
makes progress. Per block about 0.3 µs (a `copy` is 0.21-0.25 µs measured); 100 k hot blocks cost about 30 ms against
their refetch at 4.2-4.7 µs each. Correctness is `copy`'s: moving a block's code within the area changes only its
dispatcher branch, which is re-relocated; links were dropped in step 1; the record's number is new and `file` enters it
everywhere. Built only if chunk 0's simulation says it adds enough (section 5).

### 3.5 Per-event cost (*estimate*)

Today a flush costs 27-50 ms once per 1 Mi placements. An eviction: `unlink_all` + `links_restored` (as today, under 20 ms
with half the blocks), L2 memset 32 MiB (about 3 ms), survivor pass over about 500 k records with an L2 insert and up to
four chain pushes each (about 60-100 ns per block: 30-50 ms), L1 clears (8 MiB of writes, about 1 ms): **about 50-80 ms,
twice per 1 Mi placements**, so about 100-160 ms per cycle against 27-50 ms today. The cycle it replaces refetched
100-280 k blocks at 4.2-4.7 µs (0.4-1.3 s). The L1 misses and relinks survivors pay after an eviction are a few
thousand each (a few milliseconds).

## 4. Expected gain and overlap with the pool (*estimates*)

The live set under the present policy oscillates between 0 and C (mean C/2); under halves between C/2 and C (mean
3C/4), the same as raising capacity by half with no memory. How many of the 1.155 M and 2.83 M refetches this removes
depends on how soon an evicted block is wanted again, which no existing counter gives; chunk 0 measures it by replaying
the real placement sequence. Bracketing:

| | configure | make -j4 |
| --- | --- | --- |
| upper bound (every refetch removed), host paths as today | 4.9 vCPU-s (4.1%) | 13.3 vCPU-s (2.2%) |
| halves alone at 40-60% of refetches removed | 1.9-2.9 s | 5.3-8.0 vCPU-s |
| halves with carry-over at 70-90% | 3.4-4.4 s | 9.3-12.0 vCPU-s |
| the same with the pool in place (host part 2.9/3.4 -> about 0.75 µs; saving per refetch 2.0/2.5 µs) | halves 0.9-1.4 s; with carry-over 1.6-2.1 s | halves 2.8-4.2; with carry-over 5.0-6.4 |

Configure is serial, so vCPU-seconds are about wall seconds. The overlap with the cross-vCPU pool is the host part of
each avoided refetch: the pool takes roughly half of this item's gain, as the review's "take 30-40% after item 5"
anticipated. Not fixed: first-ever compiles, per-process instances (two thirds of all placements), revalidation and
shadows, and the cost of a refetch that survives both.

## 5. Chunks, gates, tests

**Chunk 0, counting (scratch, kept in `natemu-raw/io5-bins/`, not committed; one profile run of configure and make, scheduled
by the main session).**
- Host: a trace of every translation request, `(vCPU, answer fingerprint, the vCPU's `Control::blocks` and generation
  at the request)`, written beside the timing log (about 16 bytes x 1.7 M requests per vCPU). The host maps the lookup
  area already, so the fill point is one load. Instances and shares between two requests appear as the gap in the fill
  point, which transfers to any policy (they follow the guest, not the policy).
- Resident: at each capacity flush, the live blocks of the older half with `checked >= ` the epoch at which the newer half
  began (what carry-over would keep), and the same for the newer half; and the count of pages posted to this vCPU whose
  chain was empty (spurious posts, for the code-page-bit decision).
- Offline (`natemu-raw`, Python): replay the trace under today's policy (must reproduce the 1.155 M / 2.83 M within a few
  percent, which validates the model), two halves, halves with carry-over of the hot set, 2 x capacity, four regions.
  Report refetches per policy per step.
- **Gate to build chunk 1:** halves remove at least 35% of refetches on either step in the replay. **Gate for chunk 3:**
  carry-over adds at least 15 points over halves. Below both: record and stop (counters kept).

**Chunk 1, the halves (resident; host `prepare` minimum and the perf log line).** As section 3.1-3.3, with `evictions`,
`evict_us`, per-half `used`/`peak`/`capacity` in the `k1` line and `tools/io5_breakdown.py`. The census counter "hit for an
answer placed before this vCPU's last flush" is kept as a permanent counter (it is the gate's reading).
- Tier 0: unit tests in `natemu_abi` for the half arithmetic (each half holds `MAX_BLOCK_BYTES`, the largest record and at
  least one number; the halves tile the pools exactly; the code midpoint is 64-byte aligned); the resident checkers.
  Under `link-check`, an `evicted_clean` self-check after each eviction: no L2 entry, chain head, chain link or L1 owner
  names a number in H, and no record exists.
- Tier 1, K1 (natemu against QEMU, 1 and 4 vCPUs, `--code-cache` 64 KiB so a half holds a few hundred blocks and each
  case runs in seconds; the program driver runs shared code (PIC) by default, so instances are exercised; `stats.evictions`
  asserted at least 2 as `pic-flush` asserts its flushes):
  - `evict-cycle`: three rounds over more code pages than a half holds, values checked (the refetch and rebuild path);
  - `evict-write`: after a cycle, the guest rewrites one page whose blocks survived and one whose blocks were evicted but
    not yet overwritten, then runs both: the new bytes' effects (QEMU is the reference);
  - `evict-link`: calls from early pages into late pages across the half boundary, a cycle, the calls taken again
    (survivor into evicted target), under `link-check`;
  - `evict-peer` (4 vCPUs, the `pic-xwrite` handshake): a peer rewrites a page of this vCPU's survivors after an eviction;
  - `evict-instance`: instances at a second address of a translation placed before the boundary; the translation's half
    is evicted; the instances must not run its old code (values right; also under `--check-reuse`).
- **Mutations that must turn red** (one build each): L2 not rebuilt (`evict-write`, evicted page: old code runs); chains not
  rebuilt (`evict-write`, surviving page: the write does not kill the block); `unlink_all` skipped (`evict-link`: a branch
  into overwritten code, `link-check` halts or values wrong); instance cut skipped (`evict-instance`); the number
  validity test left as `n < blocks` (`evicted_clean` and `--check-reuse` on `evict-cycle`); eviction skipped and the half
  simply refilled (every case).
- **Tier 2 gate** (one profile, configure and make, against the group baseline): `capacity_flushes` 0 on both steps and
  `evictions` about twice the old capacity flushes; the refetch counter down by the replay's share, within 10 points;
  `host` bucket down by at least 70% of the low estimate of section 4 for halves; no other bucket up by more than half
  the gain (`miss` and `link` are the ones to watch: L1 and links after each eviction); `evict_us` under 0.5 s per vCPU
  per step; spurious posts under 1% of drains.

**Chunk 2, measurement.** Group H of the review with the pool: base / pool / pool + halves, three interleaved rounds, wall
judged on the group; then the standing suite once before merge and the benchmark round at completion.

**Chunk 3, carry-over (if chunk 0's gate passes).** Section 3.4, `carried` and `carry_us` counters; the K1 cases above run
again with a hot loop spanning the boundary (`evict-hot`: a block entered every round must never be refetched: host
requests for it counted 1); mutation: carry-over writing the record before relocating the exit (red on `evict-hot`).
Gate: refetches down by the replay's added share within 10 points; `carry_us` under 0.3 s per vCPU per step.

## 6. Stop rule and risks

- Chunk 0: halves below 35% of refetches on both steps -> stop, record; counters stay.
- Chunk 1: the Tier 2 gate missed after one round of diagnosis -> revert, keep the counters, record why. Any
  `BAD_LINK`, `link-check` halt, `evicted_clean` failure or shadow-mode disagreement traced to the design (not a slip)
  stops the item for re-review.
- Chunk 3 only on its gate; dropped if it misses its own.
- Risks: (1) the generation bump at eviction makes every reference held across it re-lookup; a path that halts rather
  than retries on a generation change would need the finer "block gone" test (none found in `kept`, the linker,
  `after_system`, the step path, but the implementer must grep every `generation !=`). (2) Code-page bits kept as a
  superset: measured, not argued away. (3) Two thirds of the fills are per-process instances; the eviction rate is set by
  instance churn, which this plan does not reduce (a later lever: instances not consuming an index number, or the pool
  making templates cheap enough to not share). (4) The replay's transfer assumption (instance counts between requests do
  not depend on the policy) is approximate; the Tier 2 counter, not the replay, is the acceptance reading. (5) The relink
  plan's suspended records, if built, are dropped at an eviction like all records: consistent with a flush, stated here so
  the two plans agree.

## Questions for the owner

1. Approve the two-halves policy as the design, with carry-over behind chunk 0's count?
2. Larger areas (about 300 MB more per vCPU at peak) stay off the table unless the halves leave the refetch share above a
   quarter of host hits: agreed?

# Architecture review: where natemu's remaining time goes, and what to change

November 13, 2026, branch `perf-real-workload`, after decisions 1-11 of [io5-perf-notes](io5-perf-notes.md). Read-only
review; no code or VM was touched. Evidence: the per-vCPU `vcpu` lines of `natemu-raw/chunk1b-profile-1/perf-{configure,make}.log`
(after link capacity; summed over the four vCPUs), the clean `io5-round-20261113-01` runs (base and chunk 1), the
perf-plan chunks 9, 19, 22, 24 and 28, the root-retention, cross-vCPU, relink and PIC plans. Every number marked
*estimate* has its arithmetic beside it; everything else is a bucket sum from the logs.

## 0. The two profiles in one table (busy vCPU-seconds, buckets exclusive; per-event cost)

| Bucket | configure (117.3 busy; serial) | make -j4 (573.5 busy; wall 139 s) | What one event is |
| --- | --- | --- | --- |
| guest | 36.9 (31%), 88.5 M runs, **417 ns per run** | 357.5 (62%), 305 M runs, **1,172 ns per run** | translated code between two exceptions |
| host (translation requests) | 21.5 (18%), 4.35 M, 4.9 us | 44.7 (7.8%), 6.85 M, 6.5 us | VM exit, host work, VM entry |
| link + patch | 9.9 + 2.2 (10.3%), 27.6 M, 358 + 138 ns | 35.1 + 7.8 (7.5%), 66.5 M, 527 + 159 ns | a taken, unlinked exit: trap, lookup, patch |
| miss | 7.7 (6.5%), 31.9 M, 240 ns | 37.9 (6.6%), 132 M, 287 ns | computed exit not in L1: trap, lookup, refill |
| abort + shadow | 5.3 + 4.7 (8.5%), 15.2 M, 349 + 311 ns | 14.4 + 10.9 (4.4%), 41.8 M, 345 + 261 ns | stage-1 fault, guest walk, shadow install |
| tlb | 4.0 (3.4%), 54.8 k, **73 us** | 12.6 (2.2%), 39.6 k, **318 us** | a CR3 load or global flush: L1 emptying, link undo |
| enter + enter_copy | 3.7 + 0.8 (3.9%), 62 M, 60 + 14 ns | 13.4 + 3.0 (2.9%), 201 M, 67 + 16 ns | the fixed cost of entering the resident |
| lookup + revalidate + fetch | 2.7 + 2.4 + 1.9 (6%), 98 M / 23 M / 13.4 M | 10.0 + 6.8 + 1.7 (3.2%), 275 M / 55 M / 12 M | L2 probe, currency walk, fetch walk |
| delivery | 3.8 (3.2%), 2.54 M, **1.5 us** | 3.4 (0.6%), 2.68 M, 1.3 us | a guest exception (#PF) delivered |
| irq | 2.8 (2.4%), 44.9 k, **62 us** | 2.0 (0.3%), 161 k, 12 us | an interrupt |
| system + helper | 2.6 + 1.3 (3.3%) | 3.0 + 6.1 (1.6%) | system instructions, helpers |
| reuse + place | 2.0 + 0.8 (2.4%), 9.2 M copies | 1.4 + 1.4 (0.5%) | in-VM copies/instances, placement |

Two numbers frame everything below. **The guest runs 0.42 us (configure) or 1.17 us (make) between exceptions**: at the
M4's clock that is roughly 1,700-4,700 cycles of translated code per trip into EL1, and on configure the trips cost more
than the code they separate (80 s of handlers for 37 s of guest). **Nearly every trip is a consequence of an address-space
change**: configure's 51 k CR3 loads are followed by 32 M misses, 23 M revalidations, 16 M links remade (14.4 M undone),
15 M aborts, 9.2 M instance copies, 3.4 M host requests: about 1.5 ms of handler time per CR3 load (80 s / 51 k), 620
misses and 300 shadow installs per load.

## 1. Root causes

### A. Currency is per address space and per block, rebuilt from zero for every process (the largest cause)

Mechanism: a CR3 load moves the epoch; every L1 entry filled with a non-global block is emptied, every link into a
non-current block is undone (one code synchronization per word), and each block is current again only after its own
fetch walk. A new process (fork/exec, most of configure's and make's work) therefore pays, per block it touches: a miss
(287 ns), an L2 lookup (36 ns x 2), a fetch walk and revalidation (123 + 142 ns), its links again (527 + 159 ns each),
and per page it touches a stage-1 abort and a shadow install (606 ns); plus ASLR instances (213 ns each) for relocatable
libraries, and host requests for blocks this vCPU has never held (section C).

How much: configure miss 7.7 + revalidate 2.4 + fetch 1.9 + lookup 2.7 + link 9.9 + patch 2.2 + tlb 4.0 + abort 5.3 +
shadow 4.7 + enter 4.5 + reuse 2.0 + place 0.8 = **48 of 117 busy vCPU-s (41%)**; make 37.9 + 6.8 + 1.7 + 10.0 + 35.1 +
7.8 + 12.6 + 14.4 + 10.9 + 16.4 + 1.4 + 1.4 = **156 of 573 (27%)**. Not all of it is churn: first-ever entries and the
2.4-2.5 M genuine guest page faults (of the 15 M / 42 M aborts) would exist in any design.

Inherent or fixable: *inherent to shadow paging* is one abort per (address space, page) first touch, unless neighbouring
present entries are installed together (fault-around; A-bit question in section 3). *Inherent to translating at a linear
address* is one mapping check per (address space, page) before code placed for that address runs again. **Everything
else is granularity and per-event cost, not design:** the check is done per block, not per page (23 M revalidations on
configure for what is probably under 2 M distinct page-epochs; count pending); links are undone and remade one word and
one synchronization at a time (relink plan: nearly every link made is a link remade); each event is a full exception
with state banking (section B). Root retention (slots) was the right question asked of the wrong workload: it keeps the
state of a *returning* address space, and chunk 0 found only 31% of revalidations return (short processes are new roots).
Shrinking the cost of a *new* address space is the lever for configure and make.

### B. Every event is an exception into EL1 with full banking and full synchronizations

Make takes about 327 M trips (misses 132 M, helpers 65 M, links 66 M, aborts 42 M, system 13 M, host 6.9 M, deliveries
2.7 M). Entering alone (enter + enter_copy) is 16.4 vCPU-s on make and 4.5 s on configure before any handler runs;
chunk 9 measured about 990 cycles per entry before its fix, of which 550 were copies, and the review of decision 11 finds
about 60% of a current-block miss still avoidable (state copied that the miss never reads; barriers for a refill that
changes no code). A link pays three full code synchronizations where one suffices (and, with CTR_EL0.IDC/DIC on this
core, cheaper ones).

How much (*estimate*): fast miss path, 287 + 83 -> about 170 ns all-in (the 60%): make 132 M x 0.2 us = **26 vCPU-s
(4.6%)**, configure 32 M x 0.2 = **6.4 s (5.5%)**. One synchronization per link instead of three: make 66.5 M x about
0.2 us = **13 vCPU-s (2.3%)**, configure 27.6 M x 0.15 = **4 s (3.5%)**. Batched restores at TLB events (relink step 1):
60-70% of `tlb`: make **8 vCPU-s**, configure **2.5 s**. Fixable entirely inside the resident; nothing in the EL1/HVF
choice forces the present cost (chunk 22's SYSCALL fast path is the proof: 40-57 ns per trip removed by banking less).
The remaining per-exception floor (exception entry/return, pipeline drain, the resident's cache and predictor footprint)
is inherent to the EL1-resident design and also taxes the *guest* bucket as cold misses after each return; the sampler
can see it (section 2).

### C. Translation requests leave the VM, and four private caches are flushed whole

Configure's host bucket is 21.5 s for 4.35 M requests, but only 0.6 M are compiles (fexc 4.8 s); 3.18 M are memory hits,
81% of them answers another vCPU compiled. Per request: the exit and entry about 1.15 us (21.5 - 13.6 translate - 2.95
log = 5.0 s over 4.35 M), admission 0.8-1.1 us (run again on every hit), lookup 0.3, disk put 0.25, the timing log
0.7 (profile runs only). On make 44.7 s for 6.85 M requests (fexc 14.3 s, admission 6.8, exit/entry about 8 s).

Behind the hit count: each vCPU's block index holds 1 Mi records and every vCPU reached that cap (peak = capacity on all
four, 10-11 whole-area flushes per run), after which it re-fetches its entire working set from the host and re-places,
re-links and re-instances it. *Estimate* of a flush: make's 5.35 M hits over about 14 cold starts (10 flushes + 4
first starts) = 380 k refetches x 3.4 us = 1.3 s host plus about 1 s in-VM re-placement and relinking = **2-2.5 s per
flush, 20-25 vCPU-s on make (3.5-4%), 11 x about 0.6 s = 5-7 s on configure (4-6%)** as an upper bound: the counter
"hit for a block this vCPU held before its last flush" would split flush churn from process churn.

Inherent: the compile itself (FEX, 8 us per block, 4.8 / 14.3 s cold; a user's second run gets most from the disk cache,
which these runs disable by policy). Fixable: the round trip for hits (pool (b) serves them in-VM at 0.5-1 us), admission
on hits (option (d)), the whole-flush policy (a larger index or partial reclamation), and ultimately the per-vCPU cache
itself (option (c), a redesign).

### D. Guest code quality

357 vCPU-s on make (62%), 37 s on configure (31%). Section 2.

### E. Faults, interrupts and system instructions through the resident

Deliveries cost 1.5 us each (3.8 s on configure, 3.2%): a guest #PF is a CR2 write, an exception frame built by the
resident, and the IRET trip back. Interrupts cost **62 us each on configure** (2.8 s, 2.4%) against 12 us on make: an
anomaly, not yet explained (suspects: a host exit per interrupt for the timer or LAPIC, or wake-up latency charged to
`irq`). System instructions and helpers are 3.9 s on configure (3.3%), 9 s on make (1.6%), spread across kinds; chunk
24-26 already inlined the frequent ones. Fixable, each small.

### Summary of causes (configure / make, share of busy time)

| Cause | configure | make | Status |
| --- | --- | --- | --- |
| A. per-process rebuild of currency, links, shadows (granularity + churn) | 41% | 27% | fixable in design; slots not the lever |
| B. per-exception cost inside A's events (overlaps A) | about 11% of A's 41% | about 8% of 27% | fixable, in progress |
| C. host round trips and whole flushes | 18% | 7.8% | pool/(d) in flight; flush policy open |
| D. guest code | 31% | 62% | section 2 |
| E. deliveries, interrupts, system instructions | 9% | 2.5% | small fixes; irq anomaly to measure |

## 2. Guest code: how far from native, and why

**What the history says.** Against the AMD box of the phase result (same compiler, `nokaslr`, cache off, N = 1):
compute-bound loops run at or near that machine's time (cray 0.89x, 7zip 1.32x, zstd 1.45x, mem 1.69x), while
process- and control-heavy programs run at 3-4.4x (build 4.40x, sqlite 3.11x, hackbench 3.06x, avx 3.06x). FEX's
straight-line output is good: cray's three hot blocks are 1.8-1.9 Arm instructions per x86 instruction, SSE maps one to
one, and no barrier is emitted for plain loads and stores (hardware TSO is on; only LOCK operations pay `dmb ish` +
`casal`). Guest memory accesses are native loads and stores through the shadow stage-1 tables walked by hardware: zero
per-access translation cost. So the 3-4x of `build` is not instruction selection. It is structure: control flow leaving
blocks, and what the exits drag in.

**Where build's guest time goes** (chunk 24/28 sampler, EL0 samples): the dispatcher's computed-exit code 15-16.5%, exits
landing on a block-exit `svc` 2-9%, page-split checks before stores 4.4-4.7% (ceiling by removing all of them: 3.7%
wall), SIMD load-then-move under 0.1%. About a quarter of build's guest bucket is glue around FEX's code, not FEX's
code. Flag materialisation is seen in zstd's hot loops but has no measured share. The exception-driven design also
charges the guest bucket for cold instruction cache and predictor state after each of the 327 M returns from EL1 (make);
the sampler can show it as sample density on block entry words.

**Gap estimate** (*estimate*, labelled pending the native arm64 VZ run): make's guest bucket is 357 vCPU-s; if native
arm64 CPython `make -j4` lands near 160-200 core-seconds (a guess from the M4's single-core speed, to be replaced by the
measurement), translated code alone is about 1.8-2.2x native and natemu's total busy time 2.9-3.6x. Of the guest
ratio, about 0.25 of the guest bucket is measurable glue (dispatcher, exits, split checks); the remainder (about
1.4-1.6x) is FEX's selection, flags and the post-exception cold state, in unknown proportions.

**Most likely codegen-level causes, ranked by evidence:**
1. Computed exits (every `ret`, indirect `call` and `jmp`) through the dispatcher's L1 code table: 15-16.5% of build's
   EL0 samples, mostly the indirect branch itself (misprediction), not the compare. Fix candidates: a one-entry inline
   cache per exit site (zero cost at monomorphic sites; linker-scale work) or the PAC-protected return-address cache
   from the backlog. Both need a census of targets per exit site first.
2. Exits that trap: the `svc` exits (system instructions, helpers, block ends without a link): already shrinking with
   each inlined instruction; the fast miss and link paths (section 1B) shorten what each costs.
3. Store split checks: 3 words per store wider than a byte; exact elision only for 8-aligned stack stores (at most 2.4%
   of build).
4. Flags: deferred and materialised at reads; unmeasured. The sampler's word classing (`codeq28.py`) can class
   flag-materialisation words in one run.
5. Register allocation across blocks is not a suspect: FEX pins guest registers to host registers; block entry/exit
   costs are the dispatcher's, counted in 1.
6. Memory model: not a suspect (hardware TSO; `locks` is the only benchmark where `dmb` + `casal` shows).

**What localises it:** the existing sampler (`natemu-raw/breakdown-20261105-01/bin/prof`, `scratch-prof.diff`,
`prof24.py`, `profcls.py`; `codeq28.py` in `codeq-20261105-01`) run once on configure and once on make at today's HEAD
(one scratch build, two profile runs, about an hour): shares of dispatcher, `svc` exits, split checks, flag words, REP
blocks, and the top blocks of cc1 with their Arm-per-x86 ratios; plus a per-site computed-exit target count. And the
native arm64 number, which turns the ratio above from a guess into a measurement.

**Recommendation.** Not a FEX codegen track now. The measured guest-side items are natemu's own (dispatcher exits,
split checks), and the resident's per-exception cost leaks into the guest bucket; a FEX-internal effort would be
expensive against the byte-identical gate and the reuse-first direction, with its gain unknown until sampled. Order:
finish the resident's in-flight work (section 3, items 1-3), run the sampler and the native comparison, then decide
one bounded codegen item (the computed-exit cache is the only one measured above 1 s on build) with its census as the
gate. Revisit a FEX track only if the sampler shows FEX's own words (not glue) above 60% of guest time *and* the
native comparison puts the guest bucket above 2x.

## 3. Ranked roadmap

Gains are *estimates* in vCPU-seconds (configure is serial: 1 vCPU-s is about 1 s of wall; make: divide by 4.1 for
wall). Effort: S under a day, M 1-3 days, L a week or more, each including review and tests. Overlaps are stated so
the totals are not double-counted.

| # | Change | configure | make -j4 | Effort | Risk | Depends on / overlaps |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | **IDC/DIC-gated code synchronization + batched link restores at TLB events** (relink step 1): 60-70% of `tlb`, one sync per batch | 2.5-3 s | 8-9 | S-M | cache-maintenance ordering; `link-check` red on a mutation | in progress; prerequisite for 2, 3 |
| 2 | **Fast miss path** for a current block (like `sys_fast`): 287 + 83 -> about 170 ns | 6.4 s (32 M x 0.2 us) | 26 (132 M x 0.2) | M | banking correctness: which state a refill reads | 1; gain shrinks if 6 or 8 cut misses (count once) |
| 3 | **One synchronization per link** (the fast link path): 527 -> about 330 ns | 4 s (27.6 M x 0.15) | 13 (66.5 M x 0.2) | S-M | as 1 | 1; lowers relink's per-trap saving (see 4) |
| 4 | **Relink on revalidation** (relink step 2), after `linked_current` is counted | 1.6-4 s | 6-15 at s = 0.2-0.5 (48.9 M x 0.6 us x s) | M | suspended records treated as live or gone; capacity | 1, inbound lists (done); with 3 the saving per trap is about 0.45 us, so take 70% of these figures |
| 5 | **Cross-vCPU pool (b) + option (d)** | 3.6-6.8 + 1-3 on the residual = 4.5-8 s | 10-18 + 2-4 = 12-22 | L (b), S (d) | containment; 272 MiB (owner) | counting chunk running; (d) alone 3.3 / 7.8 if (b) stops |
| 6 | **Page-level currency**: one walk per (epoch, page) makes every block on the page with that frame current; crossing blocks need both pages | 3-4 s (revalidate 2.4 + fetch 1.9 + part of lookup, at 80-95% of walks removed) | 8-12 | M | GLOBAL/LARGE flags from the walk per page; crossing blocks; needs a count of revalidations per distinct page-epoch | none; composes with 2 (the fast path consults the page record) |
| 7 | **Fewer whole flushes**: index capacity 1 Mi -> 2-4 Mi records, or partial reclamation | 2-6 s | 10-25 (upper; flush share unknown) | S (capacity) / L (reclamation) | memory per vCPU (owner) | after 5, refetch is cheaper: take 30-40% of these figures |
| 8 | **Shadow fault-around**: install the neighbouring present entries of the table page at one abort | 3-5 s (12.7 M shadow-only aborts x 0.66 us x (1 - 1/a), a = 4-8, half the installs useful) | 8-14 | M | **A/D bits**: an install of an entry the guest did not touch must not set A spuriously if natemu sets A on install; census first | none |
| 9 | **Cheaper delivery** of guest #PF (1.5 us -> about 0.7) and the **irq anomaly** (62 us per interrupt on configure) | 1.5-2.3 + up to 2 s | 1.5-2 + under 1 | S-M | exception-frame exactness | measure irq first |
| 10 | **Computed-exit cache** (inline cache per site or PAC return cache), after the census and sampler | 2-3 s (15% x 37 s x 40-50% recovered) | 15-27 (15% x 357 x 30-50%) | L | linker-scale patching, admission of a new stub kind; HVF exposure of PAC keys | sampler run and site census; section 2 |
| 11 | **Aligned-stack split-check guard** | under 1 s | about 8 (2.4% of 357) | M | performance cliff for misaligned stacks (exact) | sampler confirms the share on cc1 |
| 12 | **Shared code area (c)** | - | 20-40 upper (per-vCPU cold start of cc1 x 4 removed) | XL, redesign | concurrent reclamation, stop-the-world | only if 5 and 7 leave the host bucket above 5% of make |
| 13 | **Root retention (slots)** | about 0.7-5.7 s (chunk 0 model) | 6.5-44 (chunk 0 model) | L | large, reviewed four rounds | deferred by measurement; revisit on the hackbench/locks model |
| - | Bigger or 2-way L1 | about 0 | under 1-2% (chunk 19: 16 -> 64 Ki gained build 1.1 s; misses are emptying, not capacity) | S | dispatcher path cost | **drop** unless the miss-cause counter (section 4) shows capacity misses above 30% |

**Totals with overlaps removed** (*estimate*; items 1-9, the resident and host work, discounted 20% for shared events):
configure about 29-40 s x 0.8 = **23-32 s of 117 busy (20-27%)**, which is about 76.7 -> 50-58 s wall; make about
95-140 x 0.8 = **76-112 vCPU-s of 573 (13-20%)**, about 139 -> 112-120 s wall. Items 10-11 add make 23-35 vCPU-s
(4-6%) if the sampler confirms build's shares on cc1. Items 12-13 are not in the totals.

**Ranking rationale.** 1-3 first: smallest, already designed, and they lower the cost of every other event so later
counts are measured against the right baseline. 5 and 6 next: 5 is in flight and the largest single configure item;
6 is new but attacks the per-block granularity directly and needs only a count to confirm. 7 and 8 need their counters
before building (flush share; aborts per table page and the A-bit rule). 9 is cheap and the irq number is suspicious
enough to look at this week. 10-11 wait for the sampler. 4 is kept behind its own counter as planned.

## 4. Test and benchmark plan (fastest sound path)

**One scratch build first, no optimisation, four counters, two profile runs (Tier 2 cost, about an hour).** It settles
the open shares every estimate above leans on, and the diff stays in `natemu-raw/io5-bins/`:
1. miss cause: current block already checked this epoch (capacity/conflict) vs needs revalidation (emptying) vs needs
   translation: decides item 2's share and kills or keeps the bigger-L1 idea;
2. revalidations per distinct (epoch, page), and crossing blocks among them: item 6's gain;
3. host hits for a block this vCPU held before its last flush: item 7's share;
4. aborts per table page per epoch, and whether the shadow install sets the guest A bit: item 8's gain and rule;
plus the `irq` bucket split by source (timer, device, IPI, wake) and the `linked_current` count the relink plan already
asks for. Run the sampler (`bin/prof` method) on configure and make in the same session. Then the native arm64 VZ
configure/make (one run each, the same snapshot steps) for section 2's ratio.

**Per change, what it must pass** (tiers from io5-perf-notes):

| Items | Tier 0 | Tier 1 | Tier 2 gate (buckets, vCPU-s, against the group baseline) | New targeted test |
| --- | --- | --- | --- | --- |
| 1 sync + batch | resident checkers; mutation: a restore skipping the batch's final sync | K1 1 and 4 vCPUs; `link-check` | `tlb` down 50% or more; no other bucket up by more than the gain | K1 program: a CR3 load with 1,000+ undone links, every source re-taken, all words right (`BAD_LINK` never) |
| 2 fast miss | mutation: the fast path returning a non-current block (red) | K1; `link-check` | `miss` + `enter` down by 60% of the estimate's low end or more | K1: miss into a block whose page was remapped between two entries; miss during a pending event; `--check-reuse` boot 0 mismatches |
| 3 fast link | mutation: patch visible before the sync | K1; `link-check` | `link` down 30% or more | reuse the link programs; cross-page link racing a cut (`pic-xwrite` handshake) |
| 4 relink | mutations from the relink plan (suspended as live; dead source) | K1; `link-check`; host check | `link` + `patch` down, `tlb` not up by more than 20% of the gain; relinked / saved under 4 | the relink plan's tests |
| 5 pool + (d) | round trip, forged header, torn ring | the seven `pool-*` programs, both block sizes | `host` down 3% of configure busy or more; pool hit cost at most 1.0 us; make not slower beyond spread | the cross-vCPU plan's chunk 3 list |
| 6 page currency | mutation: a page record surviving its frame's change (red) | K1; shadow mode with 0 disagreements | `revalidate` + `fetch` down 70% or more | K1: two blocks on one page, the page remapped to another frame between them: the second is not current; a crossing block with only one page remapped |
| 7 flushes | unit: capacity constant and record arithmetic | K1 | `host` down with capacity flushes fewer; memory within the owner's figure | none beyond counters |
| 8 fault-around | mutation: installing a not-present neighbour (red) | K1; shadow mode; A/D-bit litmus | `abort` + `shadow` down 40% or more; installs wasted under 50% | K1: a table page half present, A bits read back by the guest after one touch |
| 9 delivery / irq | mutation: a frame field wrong (red) | K1 faults; the 7,833 precise-fault cases | `delivery` down 40% or more; `irq` explained or down | existing fault runners |
| 10-11 codegen | admission tests for the new stub kind | K1 pic programs; the suites' code-shape option (fresh compile compared) | sampler share of dispatcher words down; `guest` down 3% or more on make | census program: sites by target count; ASM tests at the gate |

**Measurement groups** (Tier 3, three interleaved rounds each, VM lock held, builds on the E cores, QEMU numbers reused):
- Group R (resident dispatcher): items 1, 2, 3, then 4 if its counter passes. One baseline, one group; wall judged on
  the group. Independent files from group H, so implementers overlap in worktrees.
- Group H (host/cache): items 5 and 7, after the counting chunk. Can be timed in the same round as R as a third
  variant (base / R / R+H) since the commits are independent.
- Group F (currency and faults): items 6, 8, 9. Needs the four counters first; its own round.
- Group G (generated code): items 10-11, after the sampler; its gate is the benchmark suite at completion (existing
  suites, three rounds), not IO5 alone, because computed-exit mixes differ by program.
- Tier 4 once per group that merges, after all fixes, before push (suite-only-before-merge).

**Stop rules.**
- A change is dropped (counters kept) when its Tier 2 bucket does not fall by at least 70% of its estimate's low end,
  or when another bucket rises by more than half the gain, after one round of diagnosis.
- A group ships only if its wall time improves beyond the spread of its three rounds with nothing slower; a group that
  shows bucket gains but no wall gain is held, not merged, until the next group's round re-measures it (small gains are
  real but invisible singly; decision rule of November 13).
- Item 4 stops if `linked_current` x 0.6 us is under 1% of make's busy time; item 5 stops at the plan's 1.8 M served;
  item 6 stops if distinct page-epochs exceed 40% of revalidations; item 7 stops if flush-attributable hits are under
  25% of hits; item 8 stops if aborts per table page per epoch are under 2 or the A-bit rule forbids prefill; items
  10-11 stop if the sampler puts the dispatcher under 8% of make's EL0 samples.
- Any shadow-mode disagreement, `link-check` halt or `BAD_LINK` traced to a design (not a slip) stops the item for
  re-review, as the retention plan's rule says.
- Round end: the full standing suite, the benchmark suite (3 rounds), IO5 full boots natemu + QEMU, independent
  replication; and the applicability audit (generic x86 patterns only) over the batch.

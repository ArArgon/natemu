# M2: page faults walk only current blocks

Branch `pfchain`, on `pic32` at 4adf6c83. November 22, 2026.

[Sharing 32-bit code](m2-pic32-findings.md) made the i386 build faster but made each page fault slower to deliver. To
deliver a #PF, natemu finds the blocks on the faulting page by walking a hash chain of block records, and those chains
had filled with records that were not current, mostly instances left by earlier processes at the same addresses. Of
481 million chain visits in one boot, 480 million were of such records, and delivery took 21.1 s of the i386
configure's 86 s of busy vCPU time. Cutting a block that is not current does nothing: it cannot be entered without a
fetch walk, which checks it again anyway. This branch keeps those blocks off the chains.

**Result** (M4, 4 vCPUs, one run each, seconds). "Before" is `pic32`: its timed numbers (bench case `io5-solo32`, at
44d480e2) and its profiled run with the chain counters (bench case `io5-solo32-profile`, at 3abd18d1; between that
commit and this branch's base the only resident change is a stricter link-point check). "After" is this branch at
eb4ef43b in the same two cases:

| Step | before, timed | after, timed | before, profiled | after, profiled |
| --- | ---: | ---: | ---: | ---: |
| 64-bit configure | 58.0 | 54.5 | 56.1 | 56.7 |
| 64-bit `make -j4` | 89.5 | 89.0 | 89.8 | 89.2 |
| i386 configure | 82.5 | **59.5** | 81.0 | **59.4** |
| i386 `make -j4` | 108.1 | 107.0 | 109.5 | 107.1 |

The i386 configure is 28% faster (23.0 s), in both cases. The other steps moved by no more than the runs of one build
differ from each other (the 64-bit configure: 58.0 and 56.1 s before, 54.5 and 56.7 s after), so they are unchanged
until more runs say otherwise. The i386 configure is now 1.09 times the 64-bit one; before this branch it was 1.42.

## The design

A block is current when natemu may use its translation without a fetch walk: its `checked` epoch is the vCPU's, or it
is global and was checked since the last global flush (`cache::current`). Before this branch a #PF or INVLPG walked the
page's chain and cut every live block on the page, current or not, and only dead blocks left the chain.

Now (`crates/runtime/src/cache.rs`):

- **The walk** (`on_linear_page`) cuts only current blocks on the page. Afterwards it reads each block again, and every
  block that is dead or not current, a block it has just cut included, leaves that chain and gets a flag for it: `OFF0`
  for its first byte's page's chain, `OFF1` for its last byte's (flag bits 13 and 14, which were free).
- **A revalidation** (in `lookup`, the one place a block becomes current again) puts the block back at the head of each
  chain whose flag is set, and clears the flag (`relist`). The flag stops a second push onto a chain the block is still
  on, which would make a cycle.
- So a chain holds the current blocks, plus those that stopped being current since its last walk. A block that is not
  current is reached only through a fetch walk, which revalidates it (and relists it) or places another block.

**Why it is exact.** A fault or INVLPG of page P must leave nothing with a byte on P that can run without a new fetch
walk (SDM Vol.3 4.10.4.1). Two invariants carry the argument:

- No L1 entry, and no active link from another page, names a block that is not current. This already held.
- Every live current block is on each of its chains. This is new: a placement files a block on its chains, the only
  other way to become current is the revalidation, which relists, and a walk drops only blocks that are not current.

A block entered without a walk was reached through L1, a link from another page or the system-call fast path, all of
which name current blocks only, then perhaps through local links, whose targets lie on their sources' pages. So the
block first reached was current at the fault; by the second invariant the walk met it and cut it, and it cannot have
been entered. That is a contradiction, so nothing on P runs without a walk.

**Behaviour change.** A stale block's suspended link records, which a cut used to drop, now stay until the block is
relinked, dies or the cache is flushed. That is still exact: a relink patches only into a block a revalidation has just
made current.

**Checks and counters.** In `link-check` builds, a chain walk halts instead of looping once it takes more steps than
there are blocks. `check_chains` runs after every INVLPG or #PF cut and at the start and end of every TLB flush. It
halts if the chains do not hold exactly the (block, chain) pairs whose flag is clear, dead blocks included, if a live
current block has a flag set, or if a block off a chain still has an L1 entry or an active link from another page.
Only the mutation runners build `link-check`, so campaigns never pay for it. `perf` builds count the drops
(`chain_dropped`) and relists (`relisted`), and count a chain visit as stale by `current()`.

## Reviews

- **The design review** found the design exact as written. It asked for four things, all built:
  - Bound the walks in `link-check` builds, so that a chain cycle fails instead of hanging.
  - Avoid four traps: test the block again after the cut; relist only in the revalidation's not-dead branch; not on the
    path that kills a global instance; and never relist a block that the revalidation's own walk killed.
  - Count stale visits with `current()`. The old counter compared `checked` with the epoch, which also counts global
    blocks still current after a CR3 load.
  - Measure whether the link-record area fills sooner, because a reclaim drops every suspended record.
- **The code review** found no exactness defect. It found one false alarm in `check_chains`, now fixed: it looked for
  L1 entries in tables with no context, which a global flush or a reset leaves untouched but unreachable until a
  context takes the table and clears it.

## Results

### Where the time went

Profiled runs (bench case `io5-solo32-profile`, M4), busy vCPU time in seconds summed over the 4 vCPUs
(`tests/io/io5_compiles.py`):

| Bucket | i386 configure, before | after | i386 make, before | after |
| --- | ---: | ---: | ---: | ---: |
| all busy time | 86.0 | **62.9** | 425.1 | 417.6 |
| in translated code | 27.7 | 26.6 | 306.3 | 305.4 |
| delivery | 21.1 | **2.4** | 6.4 | **2.4** |
| lookups | 3.0 | 4.0 | 6.5 | 8.7 |
| relinking | 1.4 | 1.9 | 14.9 | 16.5 |

Over the whole boot (all four vCPUs, the resident's counters):

| | before | after |
| --- | ---: | ---: |
| page faults delivered | 11.93 million | 11.93 million |
| time in them, natemu's part | 39.4 s | 6.3 s |
| block records visited on chains while delivering a #PF | 384 million | 42 million |
| block records visited on chains, all callers | 481 million | 43 million |
| of those, not current (after: counted with `current()`) | 480 million (counted by epoch) | 41.5 million |
| chain entries dropped / put back | — | 41.7 / 16.7 million |
| revalidations | 149.3 million | 149.4 million |
| links relinked / suspended | 198.7 / 213.8 million | 230.9 / 234.5 million |
| records dropped by a relink | 1,955 | 2,088 |
| link-record area fills (reclaims) | 0 | 0 |
| vCPU time, whole boot | 1,076 s | 1,035 s |

- With the `current()` count, almost every stale visit left is a one-time drop: 41.5 million stale visits against 41.7
  million drops. The before run's count compared with the epoch, so it includes global blocks that were still current.
  Only 0.9 million of its visits were current by the epoch, so its stale count by `current()` lies between 479.7 and
  480.7 million. Either way, about 438 million visits a boot are gone, and 33 s of page-fault time.
- Lookups and relinking took 5.3 s more in the i386 configure and make together. A stale block's suspended links now
  survive, and are relinked when the block is revalidated, instead of being dropped by a cut and made again by the
  linker. Relinked links rose 16%.
- The record area did not fill in either run, so there is no sign of the churn that would call for the fallback (kill
  an instance that is not current, instead of dropping it).

### Tests

All on the M4 at this branch:

- **Unit tests:** the Rust suite passed (633 passed; 2 known-bug results from a virtio block test already listed as
  flaky under load). The Python k1 and layout tests passed.
- **New k1 programs** (`tests/system/run_k1.py`; natemu only, so they are in every k1 case):
  - `pf-relist`: a block on V is run and made stale by a CR3 reload, called again (revalidated while still on its
    chain), and made stale again. An INVLPG of V drops it without cutting it, and a call relists it. Then `pf-remap`'s
    sequence: V made not present, a load faults, and the handler maps the second frame. The call must run the new
    bytes.
  - `pf-relist-cross` and `pf-relist-straddle`: the same with the INVLPG and the #PF on a crossing block's second page
    (its second chain), at d + 0x3ffe and at 0x1ffffe. The script asserts that the two pages' buckets differ.
  - `pf-relist-global`: V global under PGE, made stale by a global flush, with a CR3 reload before the #PF. The block
    stays current through the reload, so the #PF must cut it, not drop it.
  - `pf-relist-large`: a block in a 2 MiB page, the INVLPG and the #PF at another 4 KiB page of it.
- **Mutations** (`tests/system/run_relink_mutations.py`):
  - `pfchain-no-relist` (no relist): on a `link-check` resident, all five programs, at both block sizes, halt in
    `check_chains`; on a plain resident, all five run the old frame's bytes, so the values alone catch it.
  - `pfchain-no-off-guard` (the flag test ignored, so a block is pushed twice): all five halt in the link check
    instead of hanging.
  - `pfchain-cut-by-epoch` (the cut tests `checked` against the epoch instead of `current()`): `pf-relist-global`
    runs the old bytes.
  - Both baselines, plain and `link-check`, are green, including every relink program and `link-full`.
- **Regression:**
  - All 14 k1 cases (every program, 1 to 4 vCPUs, both block sizes, the reuse check on), `k1-full-links`,
    `frame-alias` and `frame-randomization` (both block sizes), `k2-xmc` and `k2-coherence` passed.
  - The relink, fast-miss and pool mutations are red, except as follows:
    - `invlpg-cut-keeps-suspended` is now an equivalent and is listed as not testable. It was red at 4adf6c83, but a
      cut now reaches current blocks only, and a revalidation has already relinked or dropped every suspended record
      into a block before it is current.
    - Three fast-miss mutations name source patterns already missing at 4adf6c83.
    - Two survivors that this branch does not touch: `do-prune-tlb-keeps-slot` (its code is built only for `pic-count`)
      and the pool's `no-tail-check`.

## What is left

- **The gate and the campaigns** (ltp32, torture32, the 64-bit torture, `l4a-stress` and `x3-stress` on SMP) have not
  run on this branch. They wait for `pic32` to merge.
- **One run per number.** The i386 configure moved by far more than the runs of one build differ; the other steps did
  not move beyond that spread.

## Appendix: design and review

The design note and its review were working files of the November 22 session. Their content is summarized above: what
current means, the drop and relist rule, the exactness argument and its two invariants, the options rejected (killing
stale instances; cutting by frame; counting only current entries; buckets stamped with epochs), and the review's four
points.

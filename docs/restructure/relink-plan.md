# Relink on revalidation: keep the links a CR3 load undoes, and re-patch them when their target is current again

Status: design, not built; accepted after independent review (Fable, two rounds; section 7) (November 13, 2026), branch `perf-real-workload`; references are to `0a814e4` plus the
uncommitted `link-check` edit in `cache.rs`.

## 1. The idea and why

Today a CR3 load moves the epoch (`cache.rs:648-666`) and `prune_tlb(CR3)` (`cache.rs:2299-2337`) undoes every link in
the CR3 segment (links from another page into a non-global block) and forgets its record. Each undo is a `rewrite`
with its own full code synchronization (`cache.rs:1394-1397`, `1369-1387`). When the source's exit is taken again it
traps into the linker (`execution.rs:560-638`), which finds and revalidates the target and patches the word again
(`cache.rs:1514-1678`). The proposal: at a CR3 load, restore the word but keep the record, marked suspended, on its
target's inbound list. When `lookup` revalidates the target (`cache.rs:624-638`), re-patch every suspended inbound link
whose source is alive and whose word is still in its unlinked form, with one synchronization for the batch.

Measured (sums over 4 vCPUs; `make` from `natemu-raw/chunk1b-profile-1/perf-make.log`, `configure` from
`natemu-raw/io5-round-20261113-01/c1-a624ef4-r1/perf-configure.log`):

| | make -j4 | configure |
| --- | --- | --- |
| linker entries / links made / links undone | 66.5 M / 48.9 M / 45.9 M | 27.9 M / 16.2 M / 14.4 M |
| CR3 loads / global flushes / revalidations | 36,088 / 3,513 / 54.9 M | 50,989 / 3,401 / 23.0 M |
| `link` bucket (per entry) / `patch` bucket (per link) | 35.1 s (527 ns) / 7.8 s (159 ns) | 9.9 s (354 ns) / 2.5 s (153 ns) |
| `tlb` bucket (per event; per undone link if all undos were there) | 12.6 s (318 us; at most 275 ns) | 4.1 s (75 us; at most 282 ns) |

Links made and undone are almost equal: nearly every link is remade again and again, undone mostly by TLB events. The
`io5-round` make run (`c1-a624ef4`, 256 Ki records) shows the failure mode to avoid: 93-104 reclaims per vCPU, 116.6 M
links, a 29.8 s `patch` bucket. Suspended records must not bring that back (section 3, capacity).

## 2. Exactness

**The invariant.** A patched non-local link X -> Y exists only while Y is current (`current`, `cache.rs:555-558`). Links
out of a non-current X may stay, because X runs only after its own fetch walk or through a link that is itself valid
(`cut` doc, `cache.rs:2188-2193`). Relinking at the moment Y becomes current keeps the invariant if three things hold:
the word written is exactly what `link` would write for (X, Y) now; Y is current when it is written; and the record
then sits where every later event that ends Y's currency will find it.

**The word.** Only CR3- and TLB-segment records are ever suspended. Those come from an absolute source (a shared source
makes a checked or local link, `cache.rs:1563`, `1607-1615`) into a non-instance target (an instance target makes a
checked link), so `code_of(target)` is the target itself (`cache.rs:1022-1027`) and the word is `b` to
`block(r.target).entry()`, the same value `unlink` checks (`cache.rs:1987-1988`). The record proves the pairing:
`link` refuses a source whose context differs from the target's or whose link point does not name `target.rip`
(`cache.rs:1517-1523`). Context, `rip`, `source_gpa`, link points and the code address are fixed for a block's life,
so they hold at relink time. One exception: in `pic-count` builds `link` goes through a counting stub
(`cache.rs:1618-1627`). Suspension is compiled out there, and `prune_tlb` keeps its drop path.

**A different address space.** A link's validity does not depend on the space it was made in. It depends on three
things: X's exit RIP, the shared context, and Y being current now. Y current means the walk just done for Y found every
one of Y's linear pages mapped to Y's frames (`cache.rs:590-593`, `612-626`). So when X runs (in a space where X itself
is valid, by the invariant above) and reaches Y's RIP, Y's bytes are what the fetch would return. Suppose another
process maps the same linear page to the same frame. Then Y is current there and the link is right. Suppose it maps a
different frame. Then Y's walk fails and Y is not revalidated (`cache.rs:640`), so nothing is relinked. The linker could
pick a different current block Y' for the same RIP and context only if two frames were both valid there in one epoch.
That happens only when the guest changed a PTE without invalidating it. Either translation is then permitted (stale
TLB), as for a link made earlier in the epoch today.

**Linker refusals** (`execution.rs:608-624`: past a system instruction, a step block, an armed breakpoint, an STI-first
self-entry) depend only on the (X, Y) pair but the breakpoint, so no record exists for a refused pair; arming a new
breakpoint flushes (`execution.rs:2470-2475`, `flush` `1324-1337`), dropping every record, suspended ones too.

**Where Y becomes current.** `checked` is set only by placement (a new block, empty inbound list: `cache.rs:752`, `981`,
`1163`, `1211`) and by `lookup`'s revalidation (`cache.rs:626`); `cut_one` clears it (`cache.rs:2251`). A suspended
target cannot become current through `global_epoch` either: at a global flush `global_epoch = epoch`
(`cache.rs:652`), so no `checked` reaches it. Relink therefore goes in one place, right after `lookup` writes `checked`
and the new `GLOBAL | LARGE` flags (`cache.rs:626-633`). Running it after the walk matters, because the walk may kill
blocks, the sources among them (`cache.rs:606`, `612`). It runs in every `lookup` caller (linker `find`, `kept_system`,
`sys_fast`'s after-path, `execution.rs:221`, `500`, `735`); each is a resident path with no armed stop, since every
resident entry disarms first (`cache.rs:1415-1419`). Relink halts if `armed != 0`. `sys_fast`'s probe uses
`current_at`, with no walk and no revalidation, so it is unaffected.

**Per-case outcomes.**
- *X dead.* Its death runs `prune(CHECKED, false)` over every segment (`cache.rs:2197-2207`). Suspended records whose
  source is dead are dropped there, with no rewrite. Relink also skips any record whose source is dead.
- *X's code reused.* Code and block numbers are reused only after a flush (`reset`, `cache.rs:202-249`), which comes
  after `unlink_all`, and that drops every record.
- *X's point linked again elsewhere meanwhile.* For example, X -> Y' was made while Y was away. The rule: relink only a
  word that is unlinked (`unlinked`, `cache.rs:1495-1498`, for the link point whose `offset` is `r.offset`); otherwise
  drop the record. A word that is unlinked has no live record, so each word keeps at most one live record.
- *Y killed by SMC while suspended.* `cut(.., dead)` walks Y's whole inbound list (`cache.rs:1945-1959`) and drops the
  suspended records without `unlink`. `cut_one` enters that path only if `INBOUND` is set (`cache.rs:2240`).
  Suspension leaves `INBOUND` set and relink sets it, as `link` does (`cache.rs:1644-1646`).
- *INVLPG or #PF cut of Y* (`cache.rs:2122-2154`). `unlink_inbound(n, false)` drops Y's suspended records as well. The
  link-check post-condition (`check_inbound`, `cache.rs:2214-2227`) stays as it is: after a cut there is no record
  below `links_tlb` on the list. Keeping them would also be exact, since the next revalidation walks again, but it
  would need a new post-condition.
- *Global vs non-global.* If Y's walk now finds `GLOBAL`, the relinked record goes into the TLB segment, which is what
  `link` would choose (`cache.rs:1611-1612`). `LOCAL_INBOUND`, local links and checked links are never suspended, since
  no TLB event touches them.
- *Capacity reclaim and `unlink_all`.* Every path that calls `unlink` on a record must skip suspended ones: `prune`,
  `prune_tlb`, `unlink_inbound`, `unlink_all` (`cache.rs:2267-2294`, `2299-2337`, `1945-1959`, `1962-1974`). A missed
  one fails loudly, because `unlink` finds no branch and returns `None`, which halts `BAD_LINK`.

**No survivors at suspension.** After a CR3 load's `epoch += 1` every CR3-segment target is non-current: it was
non-global when linked, its flags change only at a revalidation, which needs it non-current, and every event ending its
currency removes its CR3 links. After a global flush nothing is current. So no live record stays in the scanned range.

## 3. Bookkeeping and batched synchronization

- **A fifth segment, first.** The layout becomes `[0, links_susp)` suspended, then CR3, TLB, LOCAL and CHECKED as
  today: `ends` gets five entries (`cache.rs:1831-1842`), and the ABI `Control` gains `links_susp` (abi
  `execution.rs:553-561`). With the suspended segment first, suspension moves no records at all. A CR3 load restores
  every word in `[links_susp, links_cr3)` and sets `links_susp = links_cr3`. A global flush restores every word in
  `[links_susp, links_tlb)` and sets `links_susp = links_cr3 = links_tlb`. That replaces `prune_tlb`'s compaction for
  both events. Records keep their place on their targets' lists. A flag bit in the record was rejected: suspended
  records would stay in the CR3 segment, and every CR3 load would scan all of them.
- **Relink of Y.** Walk Y's list with a `follow` cursor (the next record's index + 1, read before the current one is
  touched), as `unlink_inbound` does. Only records below `links_susp` are considered. Drop one (`remove`, which keeps
  the cursor right) if its source is dead or its word is not unlinked. Otherwise write the branch without
  synchronizing and move the record by boundary swaps, not `insert`: `insert` moves each later segment's first record
  to that segment's end (`cache.rs:1908-1924`) without fixing a cursor, so the walk could resume at an overwritten slot.
  The swap: `list_del(i)`; move the last suspended record `j = links_susp - 1` into slot `i` (`list_moved(j, i)`;
  cursor `j + 1` becomes `i + 1`); `links_susp -= 1`, so slot `j` is now the CR3 segment's first. For a TLB
  destination (Y now global), do the same once more with the CR3 segment's last record and `links_cr3 -= 1`. Write
  the record into its final slot and `list_add` it; a move whose two slots are the same is skipped. It goes to Y's head, behind the cursor, and it is no longer below
  `links_susp`, so the walk never meets it again. That is one or two record moves, and no later segment is touched.
- **One synchronization per batch.** Write all the words, then `dc cvau` each, `dsb ish`, `ic ivau` each, `dsb ish`,
  `isb`, as `synchronize_words` does (`cache.rs:1481-1492`). This is exact: the resident runs none of the code area
  before the final `isb`, and the area is this vCPU's own. Every batch goes through one function that takes the
  offsets: `synchronize_words`, suspension, relink and step 1's `prune_tlb`. Any CTR_EL0-based shortcut lives in that
  function only. With IDC, skip the `dc cvau` and issue `dsb ishst` before `ic ivau`. At suspension the words form one
  contiguous record range, so no scratch space is needed. At relink, offsets go into a 64-entry stack array, which is
  synchronized when it fills. In step 1, without suspension, `prune_tlb` makes two passes. The first restores words
  and cleans each line (`dc cvau`) without moving any record; then `dsb ish`. The second runs `ic ivau` and the
  existing compaction together, since a slot is overwritten only after it has been read; then `dsb ish`, `isb`. Only
  after that comes the gap fill (`cache.rs:2322-2332`).
- **Capacity.** Suspended records take slots out of `MAX_LINKS` (512 Ki). A relink moves a record and takes no new slot,
  so the suspended count grows only with distinct (source point, target) pairs. That is bounded by link points of live
  blocks, which on make can exceed 512 Ki (block peak 1 M per vCPU). Reclaim (`cache.rs:1590-1606`) first drops the
  whole suspended segment, which needs no code writes, only list fixups and one compaction like `prune_tlb`'s gap
  fill. Only if that leaves more than 3/4 used does today's sequence run. So suspended records never force live local
  links out. Estimate: about 20 ns per dropped record, a few milliseconds per reclaim.
- **Counters.** `suspended`, `relinked`, `relink_dropped`, a `relink` bucket, and `linked_current`. `linked_current`
  counts links made by a linker entry whose `find` neither revalidated nor placed anything (`revalidated` and
  `control().blocks` both unchanged across `execution.rs:602`). The same entries' `link` bucket cycles are summed
  separately, so the cost of the traps relink can save is measured: those entries pay no walk, unlike the 527 ns
  average. `linked_current` still counts first links, which have no suspended record. So it bounds the saving from
  above.

## 4. Cost model and policy

The trap that first reaches Y in an epoch is the one that revalidates Y, so it is not saved. Its own suspended record is
re-patched by the batch instead of by `link`, a wash. Relink saves the traps that would come *after* Y is current: loop
back edges, joins, and targets first reached by a computed exit. Labelled estimates (no measurement yet):

- A saved trap is worth about 690 ns on make (527 link + 159 patch) and about 510 ns on configure. A relink costs about
  50-100 ns (record moves, word write, its share of one synchronization). A wasted relink (never taken before the next
  TLB event) costs about 100-150 ns, counting its later restore. Break-even is about 4 wasted relinks per saved trap.
- The savable share `s` of linking traps is unknown, because the counters do not show whether the target was current
  on arrival. Make: 48.9 M x ~0.6 us net x s, which is about 6 vCPU-s at s = 0.2 and 15 vCPU-s at s = 0.5 (577 busy
  vCPU-s). Configure: about 1.6-4 vCPU-s on the same range.
- **Policy, recommended: re-patch every suspended inbound record of Y whose source is alive and whose word is
  unlinked.** "Only if the source is current too" keeps correctness for free (a non-current source cannot take the
  link) but saves almost nothing. In a loop, the back edge's source becomes current after the head, so that rule
  never relinks it. "Lazily per source" needs an outbound index, and there is none (`cache.rs:2206`). Revisit the
  policy only if `relinked / (linker entries saved)` exceeds about 4.

Order of work:
1. **Batch the restores at TLB events, and count `linked_current`.** This is worth doing alone: the `tlb` bucket is
   12.6 and 4.1 vCPU-s, mostly per-word synchronization. Estimate: 60-70% of it is saved (make about 8 vCPU-s,
   configure about 2.5). Relink also restores more words (its wasted relinks), so the batching should land first.
2. **Suspension and relink**, if `linked_current` times (its own measured cycles per entry + 159 ns patch - about
   75 ns relink) on make exceeds 1% of busy time (about 6 vCPU-s). If not, stop and record why. The break-even above is
   recomputed with that per-entry cost too: a current-target trap pays no walk, so it is worth less than 690 ns.

The accepted root retention plan (section 5.5) keeps links per address-space slot; if built, relink matters only for
loads to roots not retained. Relink is smaller, on the same inbound lists, so it can go first.

## 5. Tests and gates

Existing K1 programs (`tools/run_k1.py`, header lines 10-130), also under `link-check`: `cr3-link` (the same linear
page, a different frame in each space: each space's bytes); `remap-link`, `cross-link`, `local-gpa` (INVLPG cuts, a
crossing link, a non-local link across a CR3 reload); `link-full` (phase r10 suspends about the whole area, then
relinks it: exact counts, reclaim bound with relinks counted as links made); `global`, `place-global`, `local-write`.

New programs (natemu only):
- `cr3-relink`: two jump sites on two pages into V in spaces A (frame F1) and B (F2), K rounds. Values as `cr3-link`.
  Counts at K and 2K rounds (as `counts` does) must show 2 linker entries per round, not 4. The program ends by
  arming a DR7 breakpoint (a flush) while records are suspended.
- `relink-smc`: `dormant-write` through the two jump sites. Y's frame is written while B is loaded; back in A both
  sites run the new bytes.
- `relink-dead-source`: site J1's page is rewritten while B is loaded. In A, J2 reaches Y (relink) and J1's new block
  runs. The program ends in B with records still suspended, so the host check sees them.
- `relink-stale`: J linked into Y1 (A, F1), then into Y2 (B, F2); back in A, the PTE is changed to F2 with no INVLPG
  and J runs (Y2 relinked). Then the PTE goes back to F1 with no INVLPG, and an indirect call to V revalidates Y1
  while J -> Y2 is live. J's word is not unlinked, so J -> Y1 must be dropped. Both outcomes are allowed by the
  architecture (stale TLB); the assertion is no halt plus the host check.
- `relink-global`: V becomes global (PGE, G set) with a CR3 reload; the relinked records must sit in the TLB segment.
- `relink-many`: `cr3-relink` with many jump sites (16 or more) into V, under `link-check`. While the records are
  suspended, the TLB, LOCAL and CHECKED segments are all non-empty: a global-target link, a same-page link, and a
  checked link out of shared code. So every boundary swap and cursor fix runs next to records that must not move.
  Every site's first run after the reload must be linked (counts), and the host check must pass.
- `cr3-link` already ends in space B with J -> Y_A suspended while J -> Y_B, made by `link` (`cache.rs:1629`), holds the
  same word. That is the legal shared-word state below, and its host check is the test.

The host check `check_links` (`crates/host/src/execution.rs:859-1030`) gains rules for suspended records. Each must
name a live source and a live, non-current target, and must not be counted among the linked points. Its word must be
unlinked, or be held by exactly one live record of the same point. The second case arises when Y's walk failed in
another space and the linker linked the word to Y'. Relink still drops such a record if Y is revalidated while the
word is held. The record-count comparison counts live records only, and records in `[links_susp, links_cr3)` must name
non-global targets.

Mutations, each with the test that must catch it:

| Mutation | Caught by |
| --- | --- |
| No unlinked-word guard | `relink-stale` (`BAD_LINK` at the next CR3 load) |
| Source-dead skip removed, and `prune` keeps suspended records with a dead source | `relink-dead-source` (host check) |
| Kill keeps suspended records | `relink-smc` (`check_inbound(dead)` halt) |
| INVLPG cut keeps them | `remap-link` and `cross-link` under `link-check` |
| `unlink_all`, `prune` or `prune_tlb` calls `unlink` on a suspended record | `cr3-relink`'s flush, `link-full` (`BAD_LINK`) |
| Relinked record always put in CR3 | `relink-global` (host segment rule) |
| Relink before `checked` is written, or at the CR3 load | `cr3-link` and `cr3-relink` values |
| Relink disabled | `cr3-relink` counts |
| Relink moves records with `insert`, or skips the cursor fix | `relink-many` (a site left unlinked, or an unrecorded link: host check) |
| Host rule back to "unlinked only" | `cr3-link` (false failure: the rule must accept a held word) |

Gate: `tools/run_io5.py profile` (configure, make; VM lock) comparing linker entries, the new counters, the `link`,
`patch`, `tlb`, `revalidate`, `relink` buckets, reclaims and wall time. Pass if linker entries and link + patch + tlb +
relink time fall and reclaims stay within noise. Full suite once, before merge.

## 6. Main risk

The saving rests on an unmeasured share, linker traps whose target was already current; if most are each target's
first entry per epoch, relink saves little, so step 1's counter decides. On correctness: a path treating a suspended
record as live fails loudly (`BAD_LINK`); one treating it as gone (a dead target's list, a dead source) is silent
without the `link-check` build and the host check, which is why both gain explicit rules.

## 7. Review response (independent review, November 13, 2026: accept with fixes)

- **B1 (blocking), the relink walk could lose a record.** Accepted. With `insert`, a cursor naming a later segment's
  first record pointed at an overwritten slot. The walk then restarted at Y's head and dropped just-relinked records,
  leaving patched links with no record. Section 3 now moves records by boundary swaps (one or two moves, cursor fixed
  as `remove` does), and only records below `links_susp` are looked at. New test: `relink-many`.
- **N1, host rule too strict.** Accepted. A suspended record's word may be held by exactly one live record of the
  same point. The relink-time drop rule stays. `cr3-link`'s end state tests it.
- **N2, the gate metric.** Accepted. `linked_current` now excludes entries that placed a block. The `link` cycles of
  those entries are summed separately, and step 2's gate and the break-even use that per-entry cost. First links stay
  in the count, so it remains an upper bound.
- **N3, step-1 batching.** Accepted. `prune_tlb` restores and cleans each line in a first pass, then invalidates and
  compacts in a second, before the gap fill. One batch function serves every caller, `synchronize_words` included, so
  an IDC/DIC shortcut applies everywhere.
- **N4, global flush.** Accepted. It restores `[links_susp, links_tlb)`, then sets `links_susp = links_cr3 = links_tlb`.

## 8. As built (branch `relink`, November 14, 2026)

Step 1 (`prune_tlb` batched; `linked_current` and its link-bucket ticks) and step 2 (suspension, relink) as designed, with these
differences and findings:

- **`prune_tlb` survives only in `pic-count` builds** (suspension is compiled out there). Every other build suspends: `suspend`
  restores the words of `[links_susp, scope)` in the same two-pass batch (clean and barrier, then invalidate) and moves no record.
  `synchronize_words` (arm and disarm) and relink's batch go through one `synchronize_offsets`; the CTR_EL0 shortcuts stay in
  `clean_pou`, `clean_barrier` and `invalidate_pou` (and `synchronize_code`).
- **Drain fix (fixed outbound slots, one per link point).** A point has one slot but may have several records (a live one and
  suspended ones into other blocks: `cr3-link` alternates two targets through one site), so a suspended record is named by **no**
  slot: `suspend` clears the slot (`out_del`), `resume` sets it again (`out_add`), `list_moved` updates a slot only when it names
  the moved record, `remove(.., SUSP, ..)` and `drop_suspended` clear none, and the host check and `link-check` name only live records
  in slots. A dead source's live records go by its slots as before; its suspended records stay until the target's next relink
  drops them (source dead), a reclaim or the target's death; the host check and `link-check` allow a dead source for a suspended
  record (a dead target never). This replaces the plan's "`prune(CHECKED, false)` drops suspended records with a dead source".
- **`cr3-link`'s end state does not hold a shared word.** The body's restore is a CR3 load and a CR4 write, so at every program's
  end all non-local records are suspended and the word is unlinked. The held-word state (J -> Y2 live, J -> Y1 suspended, one word)
  is reached by `relink-held`, which ends in a #PF before the restore; every new program ends so (`cr3-relink` after its flush).
- **Linker entries.** A `cr3-relink` round costs 4 entries (two CR3-load exits, one trap per space) with relink and 6 without,
  not 2 and 4: the CR3 loads' exits are entries too. `relink-many` is 4 against 2 + 2 RM.
- **`relink-stale` runs with the page record off** (`--page-currency off`): with the record on, the PTE changed back to F1 inside
  the epoch is not seen (the record is a TLB entry), so Y1 is never revalidated while J -> Y2 is live.
- Reclaim: `drop_suspended` first; `prune(LOCAL, true)` and `unlink_all` only while more than 3/4 remains.
- `link()` counts `relinked` beside links made in `link-full`'s reclaim bound (`run_k1.py`).
- The relink walk halts `BAD_LINK` if a stop is armed (a tripwire: no resident path that arms returns into `lookup`).
- `drop_suspended`'s records count toward `reclaimed` (with those `prune` and `unlink_all` undo); `link-full`'s bound counts relinks
  as links made.
- Review additions: `relink` halts BAD_LINK if the record's point does not carry the block's RIP or the record's offset; under
  `link-check` `suspend` halts if a record's target is current, `unlink_outbound` halts if a slot names a suspended record, and
  `check_out_slots` audits every block's out slots and every live record's naming at both ends of each TLB event. The host check finds
  the point by `r.point()` and checks the pairing (RIP, context, absolute source, non-instance target).
- `relink-moves-with-insert` is red with `relink-rot0..3`: the cursor goes stale only when V is non-global, V's local record is the
  last of the local segment and follows a suspended record on V's list, with a non-empty TLB segment; each one-shot insert rotates the
  local segment, so the four programs sweep its position (a defect `relink-many`, with S first of the local segment, cannot show).

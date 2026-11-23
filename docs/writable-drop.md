# Targeted writable-shadow drop on a code mark

Status: stage 1 and stage 2 built (November 20, 2026). Motivation: a Paper (Minecraft) server start, JVM JIT on, runs about 12x
slower under natemu than natively on the same M2 Max; with `-Xint` (no JIT) only 2x.

## Behaviour in plain words

A JIT writes machine code into memory and then runs it, again and again on the same pages. natemu must notice when
code it has translated is overwritten. It does that by never letting a vCPU keep a *writable* shadow (host page-table
mapping) of a page it holds translations from: before translating from a page, the translating vCPU (`mark`) makes sure
no vCPU still holds a writable shadow of that page.

Today the only way a vCPU can drop "its writable shadows of page P" is to drop **all** its shadows (a full shadow
reset), because nothing records which linear addresses map P. For a JIT this happens on every write-then-run cycle
of a code page. Measured during one Paper start, per vCPU: about 30-37 k full resets, of which only ~830 come from
global TLB flushes and ~4.7 k from peers' requests; the rest are the translating vCPU's own (`mark`, `writers & own`).
Each reset is followed by ~500 shadow misses as the vCPU re-faults its working set (15-17 M misses per vCPU against
~150 k real guest page faults), plus block revalidation and relinking.

The change: each vCPU keeps a bounded reverse map of the writable shadows it installed. When `mark` finds the
vCPU's own writable bit set for P, it looks P up in that map and removes exactly those shadows (both spaces, with
their TLB entries), then clears its own writable bit for P. If the map could not record some writable shadow since
the vCPU's last full reset ("lossy"), it falls back to the full reset, as now (stage 1). Stage 2 does the same for a peer's
writable shadows: instead of asking the peer to reset all its shadows (`SHADOWS`), `mark` asks it to drop page P
(below). Split locks, evictions and every other reset site are unchanged.

What it does not change: which accesses fault, the code-write path (`code_written`), the peer protocol, x86-visible
behaviour. It only replaces one full reset with a subset removal that leaves the same post-condition for page P.

## Specification

**State, per vCPU (resident-private, in the vCPU's lookup area):**

- `WRITERS`: `2^17` entries of 16 bytes (2 MiB, backed when touched): word 0 = linear page `va` (bits 63:12) | kept
  (bit 1) | valid (bit 0); word 1 = guest-physical page number (bits 31:0) | full-epoch tag (47:32) | cr3-epoch tag
  (63:48). Open addressing by a hash of the guest page, probe window of 16 slots.
- In `cache::Control`: `full_epoch` (moves at every full shadow reset, `reset_shadows`), `cr3_epoch` (moves at every
  CR3 load that kept the kept half, `tlb` with `reset_cr3() == false`), `lossy` (= `full_epoch + 1` once an insert
  failed in the current full epoch), and counters.

**Liveness of an entry:** valid, and its full tag equals `full_epoch` (low 16 bits), and (kept, or its cr3 tag equals
`cr3_epoch`). *kept* = the shadow is in the supervisor space (CPL 0-2) and in the upper half (`va >> 47 != 0`): the
only shadows a CR3 load can leave in place (`Space::reset_cr3`: the kept half's global ones; its non-global ones are
removed but treated as kept here, which only over-approximates).

**Insert** (end of `install`, both success paths, only when the installed shadow is writable): probe the window up to
its first slot that is not live and take that slot; before it, a live entry for (va, gpa) that is kept, or any one when
the new shadow is not kept, means done (a non-kept entry does not cover a kept shadow of the same va: the user space's
shadow of an upper-half va dies at a CR3 load, the supervisor space's does not); no slot that is not live: set `lossy`.
An entry past the first free slot is not looked at, so a shadow may be recorded twice (a slot before its entry died
since): an over-approximation, unmapped twice at its drop. Probing the whole window for that check cost `mem` (stress-ng
vm/mmap/fork) 4.3% on the M2 Max, against 0.5% (noise) for the first-free-slot insert (4 runs each against main).

**Drop** (`mark`, when `writers & own`): if `lossy == full_epoch + 1`, full reset (as today). Else, for each live
entry of P in its window: `tables::unmap(va)` (both spaces; TLBI VAE1 per space, `dsb ish`, `isb`) and invalidate the
entry. Then clear this vCPU's bit of P in the writable mask (SeqCst `fetch_and`) and move `shadow_generation` (as a
reset does: `install` reads an unmoved generation as "my writable bit for this page is still set"). The reset counter
does not move.

## Soundness argument

Invariant **W**: if `lossy` is not set for the current full epoch, every writable shadow this vCPU currently holds of
page P has a live entry (va, P) in `WRITERS`.

1. *Writable shadows are created only by `install` → `tables::install(.., writable = true, ..)`* (`Space::install` via
   `map`/`map_global`; `install_ones` is read-only). Each one inserts an entry or sets `lossy`.
2. *An entry stops being live only when its shadow is gone*: full tag changes only in `reset_shadows`, which removes
   every shadow of both spaces (`tables::reset`); the cr3 tag of a non-kept entry changes only after `reset_cr3`
   returned false, which removed every shadow outside the supervisor upper half (`Space::reset_cr3` clears supervisor
   lower, user lower and user upper roots). Entries are invalidated explicitly only after `tables::unmap` of their
   va. Other partial removals (INVLPG, #PF, UC, `unmap_large`) leave stale-but-live entries: extra unmaps later, never
   a missed one. A 16-bit tag that wraps can make a dead entry look live (extra unmap), never a live one dead.
3. *Entries are never evicted while live*; an insert that finds no slot sets `lossy` for this full epoch, and the drop
   then does the full reset.
4. Hence after a non-lossy drop of P this vCPU holds no writable shadow of P: exactly the property the full reset gave
   `mark` for P (its own shadows of other pages are irrelevant to P's coherence). Clearing its own writable bit of P is
   then truthful. The mark-before-read ordering of `mark` (code mark set, then the writable mask read) and of a
   writable install (bit set, then marks read) is unchanged, so a later install of P by this vCPU sees the mark and is
   read-only, and a later write to P takes the code-write path.
5. Peers: a peer that read this vCPU's bit before the clear still asks for and waits on a full reset (`SHADOWS`, reset
   counter), served by `take_invalidations` as today; one that reads it after the clear finds no writable shadow of P
   here (point 4). The bit is cleared only after the unmaps' `dsb ish`.
6. Single writer: `WRITERS` and the epochs are read and written only by this vCPU's resident, with no generated code of
   this vCPU running.

## Review (November 20, independent)

No soundness problem found in the code. Applied: the insert rule above stated exactly; `shadow_generation` moves on a
targeted drop (it was safe without, since no `mark` runs between `translate`'s `shadowed_writable` and `install`, but only
by that argument). Expected cost: a vCPU that dropped one page keeps its other writable bits, so a peer marking one of
those pages now waits for this vCPU's next `take_invalidations` (for example after a host compile) instead of finding
them already cleared by the full reset; measured as `shootdown` time. `tests/system/run_k1.py` `page-mark` counted the
mark's full resets; it now counts resets plus targeted drops.

## Stage 2: a peer drops one page

Where `mark` found a peer's writable bit set for P, it used to ask the peer for a full reset (`SHADOWS`, a kick) and
wait until the peer's reset counter moved. Now it asks the peer to drop P:

- The peer's record has eight drop slots (`Record::drops`). `ask_drop` claims a free slot with a compare-and-swap,
  writes P there, sets the `DROPS` bit and kicks the peer. With every slot taken it asks for the full reset instead.
- The peer serves its record wherever it already served `SHADOWS` (`serve_peers`: at `take_invalidations`, in the I/O
  wait loop, at the start of a split request and while waiting on a split lock). For each claimed slot it drops P's
  writable shadows exactly as for its own mark (`drop_writable`: all its shadows when its reverse map falls short), then
  empties the slot (`drop_served`).
- `mark` waits until that slot is emptied or the peer's reset counter moves (a full reset for any reason serves the
  request too), or until P's mark is cleared, as before.
- The host watchdog treats a pending `DROPS` like a pending `SHADOWS`: a vCPU with either is not idle.

The post-condition for P is the one stage 1 gives: once the slot is emptied, the peer holds no writable shadow of P, and
its writable bit for P is clear. A peer that reset in the meantime satisfies it as well. Counters: `served` (drops served
for peers) and `slots_full` (requests that fell back to `SHADOWS`).

Effect on a Paper start (per vCPU): full resets about 33 k to about 1.1 k, shadow misses about 16 M to 4-5.6 M; a traced
start 55.2 to 41.45 s. These numbers come from a scratch driver and a hand-prepared disk that no longer exist, so they cannot be
repeated; the bench case `guest-paper` (tests/bench/run_paper.py) is how a Paper start is measured now. Its first
numbers, on the same M2 Max (natemu 41e78e44, November 21, median of 3 boots): the first start of a boot 29.7 s (the
boots 29.4, 30.4 and 29.7 s; Paper's own "Done" 26.6 s), the second 29.0 s (29.3, 28.9 and 29.0 s; 26.5 s). The
[benchmarks page](benchmarks.md) has the M4's current ones. Experiment (an earlier version of the case: 8 GiB and Java's
default heap, one boot, with internet, November 20, M2 Max): without `--speculation-barriers`, so with PTI on, the two
starts took 191.7 s and 170.8 s, against 32.6 s and 35.1 s with it.

An independent review of stage 2 found no soundness problem; it listed stale comments, now fixed.

## Checks

- Unit-level: insert/lookup/liveness/epoch/lossy logic (pure functions, host-testable).
- Existing coherence tests (self-modifying code, cross-vCPU code writes, faults/smp families) through
  `tools/natemu-test`.
- Paper start (default and `-Xint`), boot smoke: time and the counters (targeted drops, lossy fallbacks, full resets).

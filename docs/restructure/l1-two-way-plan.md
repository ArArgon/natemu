# Two-way L1: a second entry per set at today's memory

Status: design, not built (November 14, 2026), branch `perf-real-workload`; [io5-perf-notes](io5-perf-notes.md) decision
21, after the L1 count (`natemu-raw/l1-study-profile-1`, `tools/l1_study_an.py` on branch `l1-count`). Builds before the
[page-currency](page-currency-plan.md) and [fast-miss](fast-miss-plan.md) plans (fast-miss plan section 7). Line
numbers are those of `f9a85bb`.

## 1. Why

The dispatcher's L1 is direct-mapped on the low 16 bits of the RIP. On make, 88.6 M of 132.4 M dispatcher misses (67%)
hit an entry that held another current block: two hot RIPs with the same low 16 bits in one code context, taking turns.
The count shows they are pairs, not chains: the mean number of current blocks per used index is 1.34 on make (1.20 on
configure), 0.4% of current blocks sit in an index holding eight or more, and a second way at the same memory would
have served up to 87.6% of those misses (an upper bound; the exact 2 x memory variant gives 92.0%), where doubling the
direct-mapped table gives 53% and quadrupling 76%. Each such miss is a full round trip through the resident, about
0.5 us on make (section 6). Configure has 5.5 M of them, the same shape.

## 2. The L1 today

- **Dispatcher** (`crates/runtime/src/gates.S:247-253`, copied into the code area at `area::DISPATCH` by `fill_head`,
  `cache.rs:368-423`; one copy per dispatcher slot, `write_slot`, `:425-449`): with x0 = the next RIP,
  `and x1, x0, #L1_MASK` / `adrp x2, table` / `add x2, x2, x1, lsl #5` / `br x2`; the word after is `svc #MISS`
  (`DISPATCH_MISS`), where an entry that does not match branches back to. Four instructions and no data load.
- **Entry** (`l1_words`, `cache.rs:303-315`; `area::L1_ENTRY_BYTES` = 32, `L1_BITS` = 16, four tables of 2 MiB,
  `crates/abi/src/execution.rs:245-257`): eight words, `movz x2, #rip[15:0]`, `movk x2, #rip[31:16], lsl 16`, `movk ..
  lsl 32`, `movk .. lsl 48`, `sub x2, x2, x0`, `cbnz x2, +8`, `b full_entry`, `b DISPATCH_MISS`. A hit is 4 + 7 = 11
  instructions; a miss 4 + 7 and the `svc`. An empty entry's first word is `b DISPATCH_MISS` (`empty_l1`, `:330-337`).
- **Owners and lists** (lookup area, `area::L1_OWNERS`, `L1_LISTS`, `:294-300`): per entry a u32 owner (block number + 1,
  0 empty, bit 31 `L1_LISTED`); per table a list of the entries filled with a non-`GLOBAL` block since the last clear or
  CR3 pass, so a CR3 load empties only those (`tlb`, `:700-731`: empty each listed entry whose block is not current,
  `dc cvau` each, one `dsb`, compact the list, `ic ivau` the emptied, `dsb; isb`).
- **Fill** (`fill_l1`, `:519-560`): index `rip & L1_MASK`; skip if the current table's context is not the block's; list
  the entry if unlisted and the block non-global; write the owner; write the eight words unless already there; one
  `synchronize_code` over 32 bytes (`:1369-1387`: `dc cvau` per line, `dsb ish`, `ic ivau` per line, `dsb ish`, `isb`).
  Entry target: `code_of(block).full_entry()` (a PIC instance enters its translation's prologue, which writes the page
  base).
- **Empty / kill**: `cut_one` (`:2325-2332`) empties entry `rip & L1_MASK` in every table whose owner is the block and
  synchronizes each; `clear_l1` (`:339-346`) empties a whole table and its list when a context takes it (`select_l1`,
  `:488-516`) or at a global flush (`tlb`, `:688-698`); `reset` clears the current table.
- **Who else indexes L1**: `dispatch-study` builds (`count_miss`, `:568-584`; `L1_EVICTED` in `fill_l1`, `:531-537`) and
  the `l1-count` scratch build's shadow tables; the step block's computed exit goes straight to `DISPATCH_MISS`, never
  through L1 (`place_step`, `:1312-1316`); checked links branch to a dispatcher slot, not an entry; the host only reports
  `l1_switches`, `l1_clears`, `l1_counts`; `tools/run_l1*.py` are the Linux boot-protocol "L1" checks, unrelated. K1's
  `contexts` program already builds a pair at the same low 16 bits (`CQ1`, `CQ2`, `tools/run_k1.py:695-708`).

## 3. Design

### 3.1 Layout

Same memory: each table stays 2 MiB, now 2^15 **sets** of 64 bytes, each set two **ways** of 32 bytes (way 0 at +0,
way 1 at +32). The set index is RIP bits 14:0 (`L1_SET_MASK` = 0x7fff). Owner and list arrays are unchanged in size and
meaning, indexed by **slot** `i = 2 * set + way` (0 .. 2^16), so `l1_entry(t, i)` = `L1_CODE + t * TABLE + 32 i` still
names the way's code, and the lists go on holding slot indices. The owner word gains one bit: bit 30, `L1_NEXT`, kept
in way 0's word only: "the next victim of this set is way 1" (fill-order replacement). `l1_block` masks both
`L1_LISTED` and `L1_NEXT`; `empty_l1` preserves both; `clear_l1` writes 0.

### 3.2 Probe

Dispatcher: `and x1, x0, #0x7fff` / `adrp x2, table` / `add x2, x2, x1, lsl #6` / `br x2` (the mask and the shift
change; `fill_head`'s form check gains both: the `and` immediate decodes to the set mask and the `add`'s shift to 6).

Way 0 (eight words at set + 0): `movz/movk x2` x 4 (rip0); `sub x2, x2, x0`; `cbnz x2, +12` (to set + 32, the first
word of way 1); `b full_entry0`; word 7 `b set + 32` (never reached; a harmless landing).
Way 1 (eight words at set + 32): `movz/movk x2` x 4 (rip1); `sub x2, x2, x0`; `cbnz x2, +8`; `b full_entry1`;
`b DISPATCH_MISS`.
Empty way 0: first word `b set + 32`. Empty way 1: first word `b DISPATCH_MISS`.

| Path | Instructions (dispatcher included) | Today |
| --- | --- | --- |
| way-0 hit | 4 + 7 = **11** | 11 |
| way-1 hit | 4 + 6 (way 0: compare, `cbnz` taken) + 7 = **17** | 11 (if it was the entry) or a miss |
| way 0 empty, way-1 hit | 4 + 1 + 7 = 12 | |
| miss, both ways filled | 4 + 6 + 7 = **17**, then the `svc` | 11 |
| miss, way 0 empty | 4 + 1 + 7 = 12 | |

Nothing on the hit path loads or stores data (no MRU update): the replacement bit is written by the resident at fill.

### 3.3 Fill and replacement (`fill_l1`)

For set `s` of `rip`, slots `w0 = 2s`, `w1 = 2s + 1`:
1. If either slot's owner is the block: the "already there" path on that slot (compare the words, rewrite only if they
   differ); return. (Both ways are checked so a block is never in both.)
2. Else if `owner(w0) == 0`: fill w0. Else if `owner(w1) == 0`: fill w1.
3. Else the victim is w1 if `L1_NEXT` is set in w0's word, else w0; fill it and flip `L1_NEXT`. (`dispatch-study` counts
   `L1_EVICTED` here, as today.)
Filling a slot is today's work: list it if unlisted and the block non-global, write the owner (preserving the set's
`L1_NEXT` when the slot is w0), write the eight words of its way, one `synchronize_code` over its 32 bytes. Both owners
are current when step 3 runs: a non-current owner's slot was emptied by the CR3 pass or by `cut_one` already.

### 3.4 Empty, kill, clear, lists

- `cut_one`: for the block's set in every table, empty **each** slot whose owner is the block (a loop over the two
  slots) and synchronize; an emptied way 0 gets `b set + 32`, an emptied way 1 `b DISPATCH_MISS`.
- `tlb`'s CR3 pass: unchanged; the lists hold slot indices, `empty_l1(t, i)` picks the empty word by `i & 1`.
- `clear_l1`: unchanged but for the per-way empty word; `synchronize_l1` covers the same 2 MiB.
- `empty_l1(t, i)`: writes the way's empty word (`i & 1` decides) and keeps `L1_LISTED | L1_NEXT` in the owner.
- `count_miss` (`dispatch-study`): classify by both slots of the set (`MISS_OTHER` only if neither holds the RIP;
  `MISS_SAME` if one does).

### 3.5 Code synchronization

Each way lies inside one 64-byte line (the tables are page-aligned, sets 64-byte aligned), so a fill cleans and
invalidates one line as today; `synchronize_code` is the one place, and the IDC/DIC rule of item 1 (skip `dc cvau`
with CTR_EL0.IDC, skip `ic ivau` with DIC) applies unchanged. No generated code of this vCPU runs while the resident
writes a way, and each table is this vCPU's own, so a partially written way is never executed; the fall-through word
of way 0 is written before way 1 is ever filled (at clear or at way 0's own fill), so way 1 is reachable only through a
complete way 0.

### 3.6 Links, relink, PIC, the step block

- **Links** are patched branches from a block's exit to a block's entry or an `svc`; checked links branch to a
  dispatcher slot (`CHECKED` segment). None names an L1 entry; `link`, `unlink`, `prune`, `prune_tlb` and the inbound
  lists are untouched. `write_slot` copies the dispatcher's words 0, 2 and 3 from slot 0 (`:429-432`), so the new mask
  and shift reach every slot.
- **Relink** (relink plan, not built) re-patches link words in `lookup`; no L1.
- **PIC**: a way's target is `code_of(block).full_entry()` as today; two instances in one set each enter the shared
  prologue with their own x0, which writes their own page base.
- **Step block**: its computed exit is `b DISPATCH_MISS` (`:1312-1316`), unchanged.
- **The fast-miss plan's test preludes** evict a block "with a block at V + 64 KiB"; under two ways that no longer
  evicts (both fit). Those preludes need two evictors in the set (V + 64 KiB and V + 128 KiB, or V + 32 KiB and
  V + 64 KiB); noted here for that plan's edit when it is built.

## 4. Correctness

The L1 invariant today, per entry: **a filled entry names, for exactly the RIP in its compare, a live block of the
table's context that is current (or `GLOBAL` and current since the last global flush); an empty entry branches to the
miss.** Every path that ends a block's currency or life empties its entry (`cut_one`, the CR3 list pass, `clear_l1`,
`reset`), every fill writes the owner before the words, and the dispatcher only enters through an exact compare.

With two ways the invariant is per **slot**, unchanged in wording; what is new is only how a slot is reached and what
an empty slot branches to:
1. **Exact match.** Each way compares the full 64-bit RIP before entering; way 1 is reached only when way 0's compare
   failed (or way 0 is empty). So a block is entered from L1 only for its own RIP, whichever way holds it.
2. **Emptying reaches every slot.** `cut_one` loops over both slots of the block's set; the lists hold slot indices;
   `clear_l1` and `reset` cover both ways. A block can be in at most one slot per table (3.3 step 1), and a duplicate,
   were one made, would be emptied too (the loop keys on the owner, not on a single slot).
3. **An empty way 0 never hides way 1.** Its empty word is `b set + 32`; only way 1's empty word is `b DISPATCH_MISS`.
   The reverse mistake (an empty way 0 branching to the miss) costs a miss, never a wrong entry.
4. **The replacement bit changes no semantics.** `L1_NEXT` chooses which valid slot is overwritten; the overwritten
   slot's owner is written first and its words after, as today, so an overwritten way is at every instant either the
   old entry, or the new one, or (between the owner write and the words) an entry the resident will complete before
   any code runs.
5. **Contexts.** A table still holds one context's entries (`select_l1`, `dispatcher_for` unchanged), so a way-1 hit is
   as context-exact as a way-0 hit.
6. **The set index is a function of the RIP alone**, so the CR3 pass, INVLPG cuts and code-write kills find a block's
   slot from its RIP as they do today.

## 5. Per-event costs (estimates; the way-1 hit is measured in chunk 1)

| Event | Change |
| --- | --- |
| way-0 L1 hit | none (11 instructions) |
| way-1 L1 hit | +6 instructions, one more taken branch: about +1-2 ns on a 3-4 ns transfer |
| dispatcher miss | +6 instructions before the `svc`: about +1-2 ns on a 300-500 ns event |
| `fill_l1` | +1 owner load (the other way), the `L1_NEXT` read and flip: a few ns on about 60-100 ns |
| `cut_one` | +1 owner load per table |
| CR3 list pass, `clear_l1`, `reset`, `select_l1` | none (same bytes, same lists) |
| memory | none (2 MiB per table, owner and list arrays unchanged) |

## 6. Expected gain (estimate; arithmetic beside each figure)

A conflict miss into a current block costs the gate (about 70 ns, in `guest`) + `miss` 315 ns + one `lookup` probe 35
ns + `enter` and `enter_copy` 80 ns = about **0.5 us on make**, and 70 + 220 + 27 + 73 = about **0.39 us on configure**
(`natemu-raw/group-r1-profile-2`). The count's f for 2^15 sets is an upper bound (0.876 make, 0.893 configure); the
exact 2^18 direct-mapped figure (0.756 / 0.692) is taken as the floor, since the two-way table serves every pair the
larger table serves.

- make: 88.6 M x (0.76-0.88) x 0.5 us = **34-39 vCPU-s of 561.5 busy (6-7%)**, about 8-9.5 s of 139 s wall; less the
  way-1 hits' cost: unmeasured (no hit counter exists); if way-1 hits are a third of about 2.5 G dispatcher hits at
  +1.5 ns, about 1.2 vCPU-s; and the misses' +6 instructions, under 0.1 vCPU-s.
- configure: 5.5 M x (0.69-0.89) x 0.39 us = **1.5-1.9 s of 109.5 (1.4-1.7%)**, about the same in wall time (serial).

The fast-miss plan's make pool shrinks to (1 - f) x 88.6 M; its gate is recomputed there (its section 7, step 3).

## 7. Rejected

1. **Doubling the direct-mapped table** (2^17 or 2^18 entries): 53% / 76% of the conflicts for 2x / 4x the memory in
   a code area that is the capacity limit (88% full on make; 10 capacity flushes per run); the count shows pairs, which
   a second way catches at no memory.
2. **Two ways at 2^16 sets** (4 MiB per table): 92% against 88% for 8 MiB more per vCPU out of the code area (decision
   21: about 2 vCPU-s more on make for a likely extra capacity flush).
3. **MRU replacement** (promote the hit way): a store on every L1 hit, in generated code's hottest glue; fill-order
   needs one bit written by the resident only.
4. **Moving way 0 to way 1 on a fill** (always fill way 0): rewrites sixteen words and synchronizes two lines per fill
   instead of eight and one.
5. **A folded set index** (bits 14:0 xor bits 29:15, one more dispatcher instruction): decision 9's fold changed the
   index and gained nothing; kept as the one fallback if the measured f is well under the count's floor.
6. **Four ways**: chains of three or more are 7.7% of current blocks (configure) and 16% (make) by index membership,
   but a four-way probe costs every miss and every late-way hit 7 instructions more; measure two ways first.

## 8. Chunks, gates, tests, mutations

**Chunk 1: two ways (S-M: `area` constants and the owner bits in abi; the dispatcher template's mask and shift;
`l1_words` per way; `empty_l1`, `fill_l1`, `cut_one`, `count_miss`; `fill_head`'s form check; K1 programs; an independent
review before the gate).**
- Tier 0: abi unit tests (slot/set/way arithmetic; `l1_block` masks `L1_LISTED | L1_NEXT`; the way-0 words' `cbnz`
  lands on set + 32 and way 1's on its own word 7; the empty words per way); the resident checker in `fill_head`
  (template `and` immediate = `L1_SET_MASK`, `add` shift = 6, as it checks the `adrp` today); the mutations below.
- Tier 1 (`run_k1.py`, short programs, QEMU-compared where the values are fixed by x86, natemu counts where noted;
  K1 stays at its length). Blocks A, B, C, D at V, V + 64 KiB, V + 128 KiB, V + 32 KiB (A, B, C share bits 15:0; D
  differs in bit 15: the same set under the 15-bit index), each `mov eax, k; jmp r13`:
  - `l1-pair` (QEMU values, natemu counts): warm A and B; K rounds A, B by `jmp r13`: values per k; misses 0 after the
    first round (the pair fits). The same with A and D.
  - `l1-triple` (QEMU values, natemu counts): K rounds A, B, C: fill-order replacement gives exactly 3 misses per round
    (asserted); then rounds A, B, A, B after C: 0 misses after the first (C's way was the older).
  - `l1-pair-invlpg` (QEMU): A in way 0, B in way 1; B's page remapped and INVLPG; `jmp r13` to B: the new frame's bytes.
    Also with the roles swapped (A remapped).
  - `l1-pair-cr3` (QEMU): A, B warm under table A; CR3 to table B where B's RIP maps other bytes: B's own bytes; back
    under A: A's bytes (the listed-entry pass emptied the way-1 slot).
  - `l1-pair-write` (QEMU): B's frame rewritten through its data alias; `jmp r13` to B: the new bytes (the kill emptied
    B's slot whichever way held it). Then the same with A, which is in way 0 with `L1_NEXT` set.
  - `l1-global` (natemu counts): A global (PGE, G set), B not: a CR3 reload empties B's slot only; A hits after it, B
    misses once.
  - `l1-pic` (natemu): two PIC instances of one translation at ASLR aliases sharing a set: each runs with its own page
    base (`lea [rel]` values, as `reuse-alias`).
  - `contexts` extended: `CQ3` = LP + 0x1b1000 joins `CQ1`, `CQ2` so the three share a set and evict (today's pair no
    longer does), and the pair plus triple run under each of its six contexts (more contexts than tables, so tables are
    cleared and reused around them); the existing assertions stay.
  - Existing: every `run_k1.py` program at 1 and 4 vCPUs; the `pic-*` programs (instances with live L1 entries);
    `run_k2.py`; `run_slice3_vm.py smp coherence`; the `dispatch-study` build compiles and `count_miss`'s classes sum to
    the misses.
  - Way-1 hit cost: a `run_k2.py cost` row `l1way1`: a loop of computed jumps between two blocks in one set (one in each
    way) against the same loop with the two blocks in different sets; the difference in microseconds per iteration is
    the way-1 cost per transfer. Gate: under 3 ns.
- Tier 2 gate (one profile, main session): make's `miss` **count** down by at least 70% of f x 88.6 M at the count's
  upper bound, i.e. by at least 54 M; configure's by at least 70% of 0.89 x 5.5 M = 3.4 M; **no increase in capacity
  flushes** (same memory: the count is the check); `guest` not up by more than a quarter of the `miss` + `enter` saving
  (the way-1 cost); `l1`, `tlb`, `link` unchanged within noise. Wall time is judged alone for this item (decision 21:
  the largest single item of the round), three interleaved rounds.

**Deliberate mutations, each turning its test red:**

| Mutation | Caught by |
| --- | --- |
| M1 empty way 0 branches to the miss instead of set + 32 | `l1-pair` counts (B misses every round) |
| M2 way 0's `cbnz` to its own word 7 with `b DISPATCH_MISS` there (no chain) | `l1-pair` counts (way 1 never hit) |
| M3 `cut_one` empties only way 0 | `l1-pair-invlpg`, `l1-pair-write` (B in way 1 runs stale bytes) |
| M4 `l1_block` does not mask `L1_NEXT` | `l1-pair-write` second part (A in way 0 with the bit set is not found: stale bytes) |
| M5 `L1_NEXT` never flipped (always evict way 0) | `l1-triple` counts (A, B, C then A, B: the miss pattern differs) |
| M6 dispatcher keeps `lsl #5` or the 16-bit mask | `fill_head`'s checker halts; every K1 program |
| M7 lists store the set instead of the slot | `l1-pair-cr3` (way-1 slot not emptied at the CR3 load: stale bytes) |
| M8 fill's "already there" check on way 0 only | `l1-pair` counts under `dispatch-study` (`L1_EVICTED` counts a self-eviction) |
| M9 `empty_l1` drops `L1_LISTED` | `l1-pair-cr3` (an unlisted stale slot survives the CR3 pass) |
| M10 way-1 words' `b DISPATCH_MISS` replaced by `b set + 32` (a loop) | every program hangs: the watchdog; `l1-pair` counts |

## 9. Stop rule and risks

- Revert if make's `miss` count falls by less than 54 M after one round of diagnosis, with the folded set index (7.5)
  the one variant tried in that round; or if `guest` rises by more than a quarter of the saving (the way-1 cost); or if
  capacity flushes rise; or on any K1 red or `BAD_LINK` traced to the design (section 4) rather than a slip.
- Risks: f at 2^15 sets is an upper bound (the 15-bit index merges RIPs differing in bit 15 into one set), so the real
  removal may sit nearer the 0.76 floor; the way-1 hit cost is unmeasured until the `cost` row runs; the fast-miss
  plan's make gate and its eviction preludes must be recomputed and rewritten once this lands (3.6, 6).

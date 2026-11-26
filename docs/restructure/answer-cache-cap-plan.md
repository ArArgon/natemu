# The in-memory answer map's cap (design note)

natemu keeps fexc's answers in memory for the whole boot, in one map shared by every vCPU (`Answers` in
`crates/host/src/execution.rs`). A block compiled once is then found there by every later request for the same bytes,
whichever vCPU asks. This note covers how much memory the map may use. Branch `disk-cache-cap`, a separate commit from
the [compile cache's cap](disk-cache-cap-plan.md).

## The problem, measured

Measured by hand on the Mac mini M4 (16 GB), on a KDE desktop with Chromium, in one boot. These are experiments, not
numbers from a framework case.

- Of 3.72 M fexc compiles, 1.37 M compiled an answer again after the map had dropped it. The performance log counts
  these as `recompiled` (the map remembers the fingerprints of the answers it dropped).
- A warm Chromium start made 301 k fexc compiles, of which only 3.9 k were for code seen for the first time. Host time
  was 7.4 of 16.1 busy vCPU-seconds.

**The cause is the map's byte cap.** It is a sixteenth of the host's memory, at most 1 GiB (`cap_bytes`). That gives 1 GiB
on the 16 GB Mac mini and 512 MiB on an 8 GB Mac. A shard that passes its share of the cap sweeps down to 3/4 of it. The
boot above needed about 2.35 M distinct answers (3.72 M compiles less 1.37 M recompiles).
- At the counted size per answer measured on Speedometer, about 1.2 KB (1.9 GB for 1.55 M answers), that is about
  2.9 GB.
- The 1 GiB cap keeps about 0.65 M answers after a sweep.

## What is counted, and what is not

**Counted.** An answer counts `size_of::<Entry>()` (about 240 bytes, most of them the 15 gating words), plus the bytes of
its prefix. An object answer also counts its object's bytes, `size_of::<Kept>()` and room for the most link points.

**Not counted.** The bookkeeping around each answer is not counted, about 200 to 270 bytes in all, worked out from the
types rather than measured:
- the `Arc` header (16);
- the shard's `Slot` (32);
- its share of the shard's `HashMap<Key, Vec<Slot>>` bucket (about 64 at 7/8 load, about one answer per key);
- the `lens` entry of its RIP (about 80, shared by the RIP's answers);
- a `MovedSlot` and its bucket for answers that can move to another page offset (about 64);
- allocator rounding of the prefix and object vectors (about 16);
- the heap behind the admitted form (`Admitted`), which `size_of::<Kept>()` does not see.

That is about a fifth on top of the counted 1.2 KB. At 1 GiB it was 0.2 GB of resident memory the cap did not see; at
4 GiB it would be 0.8 GB.

**The PIC study's set.** The `dropped` set of fingerprints (16 bytes and a bucket for every answer dropped and not
compiled again) is not bounded. A larger cap drops fewer answers, so it shrinks.

## Design

**The cap.** It becomes a quarter of the host's memory, at most 4 GiB:
- 4 GiB on the 16 GB Mac mini and on larger Macs;
- 2 GiB on an 8 GB Mac;
- `NATEMU_ANSWER_CACHE_MB` still sets it exactly.

**Why these numbers.**
- *Ceiling, 4 GiB.* After a sweep, 3/4 of it, 3 GiB, holds the measured boot's 2.35 M answers (about 2.9 GB counted).
  The keep-the-found policy below decides what goes once the cap is reached. A ceiling twice the working set (8 GiB)
  would be half of the Mac mini's memory.
- *A quarter of the host's memory.* The memory is used only as the guest's code grows: a boot that compiles little
  keeps little. The guest's RAM and the rest of natemu need the other three quarters. On an 8 GB Mac, a 2 GiB map
  beside a 4 GiB guest is already tight, and the map's cold answers are what macOS compresses first.
- **Open question for review:** whether the cap should instead come from the memory the guest's RAM leaves free, for
  example (host − guest RAM) / 3, within 512 MiB to 4 GiB. `Answers::new` does not know the guest's RAM today; the boot
  would pass it in.

**Counting the bookkeeping.** `Entry::size` adds a fixed 256 bytes for the bookkeeping above, so the cap bounds resident
memory rather than five-sixths of it. At the same cap this keeps about a sixth fewer answers. That cost is why the
ceiling moves to 4 GiB and not to 3.

**The answer bound (`CAP_ANSWERS`).** It stays at 2^22, about 4.2 M answers, of which a sweep keeps about 3.1 M: above
the measured boot's 2.35 M. With the bookkeeping counted, the byte cap binds first for answers above about 1 KB. The
count bound still limits a flood of small answers (cuts and refusals), whose bookkeeping is most of their memory.

**The sweep.** It ranks every answer in the shard, sorts the ranks, and drops the lowest until 3/4 of the shard's share
is left. Its cost is linear in the shard's answers, plus the sort.
- At 4 GiB a shard holds about 210 k answers, against about 55 k at 1 GiB.
- A sort of 210 k (rank, size) pairs took 4 ms in an experiment on the M2 Max (a scratch program; 0.9 ms for 55 k).
- The two passes over the shard's slots read each answer's `Entry` for its size, which costs a cache miss per answer.
  So a sweep holds its shard's lock about 4 times as long as now, perhaps 10 to 40 ms, and happens about 4 times less
  often. The cost per insert stays the same. Lookups in that shard wait during the sweep; the other 15 shards do not.
- The per-shard lock waits are already counted (`Answers::waits`, in the performance log). The measurement below reads
  them.
- If the waits show, keep the size in the `Slot`, so a sweep reads no `Entry`. That costs 8 bytes an answer, and it
  is not done here.

**The keep-the-found policy is unchanged.** An answer's rank is the shard's insert count when it was kept or last found,
plus half the shard's answer count if it has been found since it was kept. So an answer found again outlives the many
never found again, whatever the cap. The bonus scales with the shard's count, so the policy is the same at any cap
(`a_found_answer_outlives_answers_never_found`). A new test checks it at the new size with bytes as the binding bound.

## Alternatives considered

- **No cap, or a much higher one.** A boot's answers are bounded by the guest's code. Without a cap, a guest that keeps
  generating code without the page's dirty bit (an interpreter or JIT writing its code once) would grow the map without
  bound. The cap stays.
- **The cap from the guest's RAM.** This is the open question above.
- **Spilling dropped answers to the persistent cache instead of compiling them again.** Kernel answers (CPL 0) are not
  stored on disk, and the 1.37 M recompiles were not split by CPL in the measurement. For user code, the disk cache
  already holds answers compiled in earlier boots. Looking a dropped answer up on disk would cost a disk hit (about
  4.2 µs) against a compile (about 20 µs plus admission). That is a possible follow-up if user-mode recompiles remain
  after the cap change.
- **A second-chance (CLOCK) sweep instead of a sort.** It would not need the O(n log n) sort or the rank pass, but it
  would change which answers go, and the current policy was tuned on a replay of Speedometer. Not done.

## Tests

The unit tests are in `execution.rs`:
- `cap_bytes_from_host_memory`: the cap for a range of host memory sizes, and the override.
- `the_byte_cap_counts_the_bookkeeping`: an entry's size includes the fixed 256 bytes.
- `a_found_answer_outlives_a_byte_bound_sweep`: the keep-the-found policy with bytes as the binding bound.

Each will be shown to fail against a mutant of the code it checks.

## Measurement for the Mac mini M4 (to schedule)

Bench case `desktop-kde-browser-profile`, at this branch and at its base:
- the warm Chromium start;
- the boot's fexc compiles and `recompiled` admissions, both in the performance log's `state.admissions`;
- host time against busy vCPU time;
- the per-shard lock waits;
- the host's peak resident memory (whether a quarter of 16 GB beside the guest causes memory pressure).

The expectation, from the numbers above: recompiles fall from 1.37 M towards the boot's own re-found answers that a
3 GiB map cannot hold, and a warm Chromium start's host time falls with them.

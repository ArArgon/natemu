# The in-memory answer map's cap (design note)

natemu keeps fexc's answers in memory for the whole boot, in one map shared by every vCPU (`Answers` in
`crates/host/src/execution.rs`). A block compiled once is then found there by every later request for the same bytes,
whichever vCPU asks. This note covers how much memory the map may use. Branch `disk-cache-cap`, a separate commit from
the [compile cache's cap](disk-cache-cap-plan.md).

## The problem, measured

Measured by hand on the Mac mini M4 (16 GB), on a KDE desktop with Chromium, in one boot. These are experiments, not
numbers from a framework case. The note does not know whether that boot had the persistent compile cache on (see
below).

- Of 3.72 M fexc compiles, 1.37 M compiled an answer again after the map had dropped it. The performance log counts
  these as `recompiled` (the map remembers the fingerprints of the answers it dropped).
- A warm Chromium start made 301 k fexc compiles, of which only 3.9 k were for code seen for the first time. Host time
  was 7.4 of 16.1 busy vCPU-seconds.

**The cause is the map's byte cap.** It was a sixteenth of the host's memory, at most 1 GiB: 1 GiB on the 16 GB Mac mini,
512 MiB on an 8 GB Mac. A shard that passes its share of the cap sweeps down to 3/4 of it. The boot above needed about
2.35 M distinct fexc answers (3.72 M compiles less 1.37 M recompiles); the map also keeps answers from the disk and
copies moved to other page offsets.

## Design

**The cap takes the guest's RAM into account.** It is half of what the host's memory leaves after the guest's RAM and
3 GiB (`answer_cap`), between the old bound (1 GiB, or a sixteenth of the host's memory when that is less) and 4 GiB.

| Host | Guest RAM | Cap |
| --- | --- | --- |
| 16 GB | 8 GiB (the desktop cases' `--ram 8`) | 2.5 GiB |
| 16 GB | 4 GiB | 4 GiB |
| 16 GB | 12 GiB | 1 GiB (the old bound) |
| 8 GB | 2 GiB | 1.5 GiB |
| 8 GB | 4 GiB | 512 MiB (the old bound) |

- The boot passes its guest's RAM to the first vCPU's compiler, which makes the run's map with that cap
  (`Compiler::share_for`).
- A compiler outside a boot (tests, the program driver) keeps the old bound.
- `NATEMU_ANSWER_CACHE_MB` still sets the cap exactly. The cap is now on the performance log's `answers` line
  (`cap_bytes`).
- The 3 GiB reserve is for macOS and the rest of natemu: four code caches of 128 MiB, the shadow pool, the fexc
  servers, the compile cache's index.
- A first draft took a quarter of the host's memory, at most 4 GiB. The design review blocked it: on the 16 GB Mac mini
  with an 8 GiB guest, it would give 8 + 4 + about 1 + about 3 GiB, the whole machine with no slack once the guest's
  page cache has touched its RAM.

**What this does and does not hold.** At 2.5 GiB, about 1.8 M answers are kept after a sweep, at about 1.45 KB each
(1.2 KB counted on Speedometer, plus the bookkeeping now counted). That is most of the measured boot, not all of it.
The keep-the-found policy (below) decides what goes, and the measurement decides whether a larger share of the host
pays.

**Counting the bookkeeping.** An answer used to count only its `Entry`, its prefix, its object, its kept form and room
for the most link points. The bookkeeping around it is now counted too, so the cap bounds resident memory rather than
about four-fifths of it.
- `Entry::size` adds `BOOKKEEPING`, 128 bytes: the `Arc` header, the shard's `Slot`, the shard's map share, and
  allocator rounding.
- An answer in the moved index adds `MOVED_BOOKKEEPING`, 96 bytes: its `MovedSlot` and that map's share.
- The reviewer measured these with a counting allocator, an experiment on the M2 Max. A map's share with its vector is
  about 90 bytes at its usual load, and about 60 more just after the table doubles.

**Vectors sized for one.** A key's slot vector and a moved key's vector were made by their first `push`, which reserves
room for four: 128 and 96 bytes of heap for one 32- or 24-byte slot. They now reserve one. Most keys keep one answer,
so this saves about 160 bytes of real memory per movable answer.

**The sweep reads no answer under its lock.** The `Slot` now keeps the answer's size, so the shard's sweep never
dereferences an answer while it holds the shard's lock.
- The size goes in the 4 bytes the `hit` flag's padding took; the flag is now the top bit of the prefix length, so
  `Slot` stays 32 bytes.
- Without this, a host under memory pressure would page compressed answers back in while every vCPU waits. Every
  vCPU's lookups spread over all the shards, so a sweep's stall is the whole VM's.
- The lens map's update uses the slot's key and length.
- A larger cap makes each sweep longer, about as many times as the shard is larger, but rarer by the same factor. The
  cost per insert stays the same, give or take the sort's log factor.
- An experiment by the reviewer on the M2 Max, at 210 k answers in a shard with resident answers, measured about 16 ms
  a sweep: the rank pass 6 ms (3 ms without the answer reads), the sort 4 ms, the retain 6 ms. At 55 k it measured
  about 4 ms.
- Each sweep's time is now on the performance log's `answers` line, as `sweeps`: [sweeps, answers dropped, ns in all,
  longest ns]. If the stalls show in a desktop's frame gaps, the next step is more shards (64 restores today's shard
  size).

**The dropped answers' fingerprints** are now hashed before their lock is taken, the lock every vCPU's fresh compile
takes (`Answers::recompiled`).

**The answer bound (`CAP_ANSWERS`, 2^22) stays.** With the bookkeeping counted, every answer counts at least about
500 bytes, so below a cap of about 2 GiB the byte cap always binds first. Above it, the count bound (about 3.1 M kept
after a sweep) limits a flood of small answers. For typical answers of about 1.45 KB, it binds first only when
`NATEMU_ANSWER_CACHE_MB` sets more than about 6 GiB.

**The keep-the-found policy is unchanged.** An answer's rank is the shard's insert count when it was kept or last found,
plus half the shard's answer count if it has been found since it was kept. So an answer found again outlives the many
never found again, whatever the cap: the bonus scales with the shard's count. A found answer that is not found again
still goes once a generation of newer answers outranks it.

## Not done

- **Bounding the `dropped` set.** The PIC study's set of dropped answers' fingerprints grows with every answer dropped
  and not compiled again, over the whole boot: about 20 to 40 bytes each. A fixed direct-mapped table without a lock
  would bound it. That is a follow-up; a larger cap drops fewer answers.
- **Smaller answers.** The 15 gating words are 120 of an `Entry`'s 240 bytes, and only the ones `read` names matter.
  Moved copies clone their prefix. Keeping only the values read, and sharing the prefix, would save about 100 bytes
  and more per answer. That is a follow-up if the measurement shows memory pressure.
- **Spilling dropped answers to the persistent cache.** Kernel answers (CPL 0) are not stored on disk. With the
  compile cache on, a dropped user answer already comes back as a disk hit. It still costs an admission, and the
  `recompiled` counter never sees it.
- **A second-chance (CLOCK) sweep.** It would change which answers go, and the current policy was tuned on a replay of
  Speedometer.

## Tests

The unit tests are in `execution.rs`:
- `answer_cap_from_host_and_guest_memory`: the cap for host and guest sizes, and outside a boot.
- `the_byte_cap_counts_the_bookkeeping`: an answer's size includes the bookkeeping, and the moved part only for one
  that can move.
- `a_found_answer_outlives_a_byte_bound_sweep`: with bytes binding and answers of unequal sizes, the sweep drops
  exactly the oldest answers never found, down to 3/4 of the bytes. It keeps the found and the newest answers, and a
  found answer not found again goes after four generations.
- `answers_report_matches_the_former_format` now also checks the line's `cap_bytes` and `sweeps`.

Each new check was shown to fail against a mutant of the code it checks.

## Measurement for the Mac mini M4 (to schedule)

`desktop-kde-browser-profile` is on the `desktop-profile` and `desktop-counting` branches, not on main or this branch.
Run it on this branch rebased onto the case, and on that base, with the case's configuration (4 vCPUs, 8 GiB guest):
- the warm Chromium start's time;
- the boot's fexc compiles and `recompiled` admissions (the performance log's `state.admissions`), and the `answers`
  line: `kept_answers`, `cap_bytes` and `sweeps`. `kept_bytes` now counts the bookkeeping, so compare answers, not
  bytes, across the two sides;
- host time against busy vCPU time;
- the frame gaps (maximum and tail), as the evidence for sweep stalls;
- memory pressure. The process's `phys_footprint`, and the system's compressions, swap-outs and swap use over the run,
  not its peak resident memory, which compressed pages leave. Any swap-out rejects the cap.

Also run the cache-on variant (`-warm`). The plain case runs with the compile cache off, and with it on, dropped user
answers come back from disk instead of being compiled. Optionally run two caps of this branch on the 16/8
configuration, 2.5 GiB (the default) and 4 GiB (`NATEMU_ANSWER_CACHE_MB=4096`), labelled as an experiment, to settle
whether a larger share of the host pays.

## Review

The design was reviewed independently (Fable) before any code was written. Two findings were blocking, and both are
adopted:
1. A quarter of the host's memory ignored the guest's RAM. The cap now takes it into account.
2. The sweep read every answer under its lock. The size is now in the `Slot`.

Also adopted:
- vectors sized for one;
- two bookkeeping constants, from the measured map shares;
- fingerprints hashed before the lock;
- a byte-binding sweep test with unequal sizes, exact counts and an upper bound on the bonus;
- the sweeps and the cap on the performance log's line;
- the measurement plan's memory meters, frame gaps, warm variant and branch note;
- wording on `CAP_ANSWERS` and on what the cap holds.

Not adopted, kept as follow-ups:
- bounding the `dropped` set;
- smaller answers (the gating values read, shared prefixes);
- more shards, decided by the measured frame gaps.

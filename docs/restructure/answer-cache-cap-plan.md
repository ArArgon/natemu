# The in-memory answer map's cap (design note)

natemu keeps fexc's answers in memory for the whole boot, in one map shared by every vCPU (`Answers` in
`crates/host/src/execution.rs`). A block compiled once is then found there by every later request for the same bytes,
whichever vCPU asks. This note covers how much memory the map may use. Branch `disk-cache-cap`, a separate commit from
the [compile cache's cap](disk-cache-cap-plan.md).

## The problem, measured

Measured on the Mac mini M4 (16 GB), on a KDE desktop with Chromium, in one boot of the `desktop-kde-browser-profile`
case (run 20261126-021552-6362, on the `desktop-profile` branch). That case passes `--compile-cache off`, so its
recompiles are the in-memory map's drops alone.

- Of 3.72 M fexc compiles, 1.37 M compiled an answer again after the map had dropped it. The performance log counts
  these as `recompiled` (the map remembers the fingerprints of the answers it dropped).
- A warm Chromium start made 301 k fexc compiles, of which only 3.9 k were for code seen for the first time. Host time
  was 7.4 of 16.1 busy vCPU-seconds.

**The cause is the map's byte cap.** It was a sixteenth of the host's memory, at most 1 GiB: 1 GiB on the 16 GB Mac mini,
512 MiB on an 8 GB Mac. A shard that passes its share of the cap sweeps down to 3/4 of it. The boot above needed about
2.35 M distinct fexc answers (3.72 M compiles less 1.37 M recompiles); the map also keeps answers from the disk and
copies moved to other page offsets.

## Design

**The cap is proportional to the guest's RAM.** For a boot it is a quarter of the guest's RAM, at most what the host's
memory leaves after the guest's RAM and a 3 GiB reserve, at least 256 MiB (`answer_cap`). There is no other ceiling.

| Host | Guest RAM | Cap |
| --- | --- | --- |
| Mac mini M4, 16 GB | 8 GiB (the desktop cases' `--ram 8`) | 2 GiB (a quarter) |
| Mac mini M4, 16 GB | 4 GiB | 1 GiB |
| M3, 8 GB | 4 GiB (its usual guest) | 1 GiB (a quarter; also all the host leaves) |
| M3, 8 GB | 2 GiB | 512 MiB |
| 8 GB | 5 GiB | 256 MiB (the floor: the host leaves nothing) |
| 16 GB | 12 GiB | 1 GiB (what the host leaves) |

**Why a quarter.** Speedometer 3.1 in a 4 GiB guest needed about 1.9 GB of answers in a map that never sweeps. Only
about a third of those are ever found again. The sweeping map kept about 0.87 GB under its 1 GiB cap, recompiling
about 2.6% of its hits: an experiment, a replay of one boot's trace. A quarter of a 4 GiB guest is that 1 GiB.
- The KDE desktop with Chromium in an 8 GiB guest compiled 1.37 M answers again at 1 GiB. A quarter of its RAM doubles
  that cap to 2 GiB.
- A map sized with the guest's RAM stays a fixed share of what the VM was given, about a quarter beside it.
- The answers kept are a function of the guest's code, not of its RAM. The guest's RAM is the size the owner chose for
  the workload, and a larger workload usually gets a larger guest.

**Details.**
- The boot passes its guest's RAM to the first vCPU's compiler, which makes the run's map with that cap
  (`Compiler::share_for`). A compiler outside a boot (tests, the program driver) keeps the old bound: 1 GiB, or a
  sixteenth of the host's memory.
- `NATEMU_ANSWER_CACHE_MB` still sets the cap exactly. The cap is now on the performance log's `answers` line
  (`cap_bytes`).
- The 3 GiB reserve is for macOS and the rest of natemu: four code caches of 128 MiB, the shadow pool, the fexc
  servers, the compile cache's index.

**How it got here.**
- A first draft took a quarter of the host's memory. The design review blocked it, because it ignored the guest's RAM.
- The reviewed version took half of what the host leaves, within 1 to 4 GiB. That ceiling was raised to 8 GiB.
- The owner then asked for a cap proportional to the VM: 4 GiB of map for an 8 GiB guest is half of what the VM was
  given.

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
- At 2 GiB (the 8 GiB desktop guest), a shard holds about 90 k answers: by the same experiment, about 7 ms a sweep.
- Each sweep's time is now on the performance log's `answers` line, as `sweeps`: [sweeps, answers dropped, ns in all,
  longest ns]. If the stalls show in a desktop's frame gaps, the next step is more shards (64 restores today's shard
  size).

**The dropped answers' fingerprints** are now hashed before their lock is taken, the lock every vCPU's fresh compile
takes (`Answers::recompiled`).

**The answer bound (`CAP_ANSWERS`) is 2^23,** about 8.4 M answers, of which a sweep keeps about 6.3 M. With the
bookkeeping counted, every answer counts at least about 500 bytes. Below a cap of about 12 GiB (a 48 GiB guest), answers
of a typical 1.45 KB meet the byte bound first. The count bound limits a flood of small answers.

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
  compile cache on, a dropped user answer already comes back as a disk hit. It still costs a host trip, a disk read
  and an admission. Each vCPU's state line now counts these (`dropped_disk_hits`, beside `recompiled`).
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
- `answers_report_matches_the_former_format` now also checks the line's `cap_bytes` and `sweeps`, and
  `vcpu_state_matches_the_former_format` the state line's `dropped_disk_hits`.

Each new check was shown to fail against a mutant of the code it checks.

## Measurement

There is no separate measurement for this change; the owner keeps serious measurements to checkpoints. It goes into a
pooled checkpoint run with P1 step 1 and fault-around, and the counters are read from that run:
- with the compile cache off (as `desktop-kde-browser-profile` runs), the fexc compiles and `recompiled` admissions
  (the performance log's `state.admissions`) and host time against busy vCPU time;
- with the compile cache on at its default cap, `dropped_disk_hits` (the disk hits that map drops caused) and the
  recompiles left, which are kernel answers (never on disk);
- the `answers` line's `kept_answers`, `cap_bytes` and `sweeps`. `kept_bytes` now counts the bookkeeping, so compare
  answers, not bytes, with an earlier run;
- the frame gaps, for sweep stalls, and memory pressure (the process's `phys_footprint`; the system's compressions,
  swap-outs and swap use).

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

The code was reviewed independently (Opus) and nothing was blocking. Adopted from it:
- an answer's size is computed once on insert;
- `answer_cap`'s comment now covers the display memory left out of the guest's RAM, and the map of a vCPU whose
  features differ;
- `MOVED_BOOKKEEPING`'s comment says that moved copies are counted as if indexed.

Not adopted: a test of a short-prefix answer swept from the lens map (the code is right; a coverage gap noted here),
and building `Compiler::answers` lazily (one map thrown away per boot).

The sizing was then redone at the owner's request, proportional to the guest's RAM (above). That change was reviewed
briefly as well (Opus).

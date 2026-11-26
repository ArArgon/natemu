# The compile cache's cap and eviction (design note)

The persistent compile cache (`crates/host/src/compile_cache.rs`) keeps fexc's answers for user-mode code on disk, so a
program's second start reuses the first one's translations. This note covers how big the cache may grow, what goes
when it is full, and the in-memory index that grows with it. Branch `disk-cache-cap`.

## The problem, measured

All of these were measured by hand on the KDE desktop on the Mac mini M4. They are experiments: no framework case runs
them yet.

- The old default cap was 512 MiB. When a run starts with the cache above 3/4 of the cap, natemu deletes packs, oldest
  written first, until the cache is at 3/4 (384 MiB at the old default).
- A KDE session alone writes 592 MiB before any app starts (measured with an 8 GiB cap). So at 512 MiB the session
  evicts its own code, and the apps' code is never kept. A second session's Konsole start got 22 disk hits against
  34.9k fexc compiles.
- At an 8 GiB cap the second session hits 98–99% of the time. That saves about 0.3–0.4 s per first app start and about
  3.6 s of shell start.

Code that the guest generates at run time (JIT output) is already left out of the cache, using the page's dirty bit,
so the cache holds only code that comes from files.

## Design

**Default cap.** Without `--compile-cache-mb` or `engine.compile_cache_mb`, the cap is the guest's RAM
(`CAP_PER_GUEST_RAM` = 1), at most a tenth of the free space on the cache folder's volume, at least 1 GiB, rounded down
to 256 MiB steps (`default_cap`). If statfs fails, the cap is the guest's RAM.

| Machine | Guest RAM | Free disk | Cap |
| --- | --- | --- | --- |
| Mac mini M4 | 8 GiB (the desktop cases) | 41 GiB or more | 4 GiB at 41 GiB, 8 GiB from 80 GiB |
| M3 | 4 GiB (its usual guest) | 16 GiB | 1.5 GiB (a tenth of the free space) |
| M3 | 4 GiB | 40 GiB or more | 4 GiB |
| any | 512 MiB | any | 1 GiB (the floor) |

**This size is provisional.** The final factor gets set later from measurement: warm-session runs, comparing hit rate
against cap. Changing it is one constant.

**Why the guest's RAM.** The bytes live on disk, which is plentiful, but every process builds the cache's index in
memory at open: about 5 to 7% of the cache's bytes (33 bytes a slot for each entry of about 750 bytes, in a table sized
to a power of two; the experiment below measured 267 MiB for 5.8 M entries). Sizing the cache with the guest keeps that
index a fixed share of the VM, about 0.5 GiB for an 8 GiB guest's 8 GiB cache.
- 1× rather than 2×: the only measurement is that an 8 GiB cap (1× the desktop's 8 GiB guest) made a KDE desktop's
  second session hit 98–99%. Each open keeps 3/4 of the cap (6 GiB there), about ten times a session's 592 MiB, and
  each run has 2 GiB to write into.
- The owner judged 2× too much.

**The floor, 1 GiB.** Each open keeps 3/4 of the cap, 768 MiB here, which holds a KDE session's 592 MiB with 176 MiB
to spare. The floor keeps the session, not a desktop's apps.

**A tenth of the free space.** This keeps a Mac with a small or nearly full disk from losing a large cap to the cache.
- Steps of 256 MiB make it unlikely that two calls made moments apart disagree; it is not impossible. A process
  computes the cap once.
- The free space does not count what the cache itself already holds. So where the tenth binds, the cap is a little
  smaller than a tenth of "free plus cache".
- On a nearly full disk the floor still gives 1 GiB; the writer survives the full disk that follows (the entries are
  lost, not the run).
- A relative cache folder that does not exist yet is measured on the working directory's volume.

**One folder, several guests.** The folder is shared by every VM, and each process evicts down to its own cap at open.
At main the cap came from free space only, the same for every process; now a small guest's boot trims a large guest's
cache to the small guest's size (a default 2 GiB boot to 1.5 GiB, evicting a desktop guest's warm packs). A guest that
needs more sets `--compile-cache-mb` or a folder of its own. No guest gets less than main's 512 MiB: the floor is 1 GiB.

**How it got here.**
- The reviewed design took a tenth of the free space within 1 to 4 GiB. That ceiling was raised to 16 GiB.
- The owner then asked for a size proportional to the VM, because the index costs memory: about 0.75 GiB at 16 GiB.

The knobs are unchanged: `--compile-cache-mb N`, `engine.compile_cache_mb` and `NATEMU_COMPILE_CACHE_MB` set the cap
exactly. The startup line `natemu: code cache: pool on, disk N MiB at DIR` shows the cap that was computed.

**Eviction by last use.** A pack's modification time now records its last use: when it was written, or when a process
last hit it.
- On a process's first hit in a pack, and again on the first hit an hour or more after its last stamp (an
  `AtomicU32` per pack holds the hour of the last one), the lookup sends the pack's number to the cache's writer thread.
  The writer thread then sets the pack's time to now (`File::set_modified`, on the read-only descriptor it already
  has). The hourly stamp is for a session left running for days, whose hot packs a newer process would otherwise take
  for unused.
- This costs one syscall per pack per process and hour, and none of it is on the compile path.
- A pack time ahead of the clock (written before the clock was put back) counts as now when sorting.
- Setting a time needs the file's owner: a pack another user wrote (a run under sudo) is never stamped.
- At open, eviction still removes other builds' packs first, then this build's packs by time: least recently used
  first instead of oldest written first.
- Packs do not change format, and old caches keep working.

**Compact index.** At open, every pack's index is read into a hash map. Before this change the map held the full
32-byte key and a 56-byte slot (32-byte sum, `usize` pack, `u64` offset, `u32` length): 89 bytes per table slot. The
map was also grown by doubling, with SipHash.
- An experiment showed this does not scale to a larger cap. It used a scratch program that copies the open's index
  build, on this Mac (the M2 Max), with the local cache's packs read nine times under distinct keys. That gives 5.8 M
  entries, which is about a full 4 GiB cache: the local cache measures about 750 bytes per entry, record included.
  Building the old index took 616 ms and 1.4 GiB peak RSS.
- The new index keys an entry by the first 8 bytes of its key. It keeps the first 12 bytes of the entry's sum, with
  `u32` pack, offset and length: 33 bytes per slot (the 4 more bytes of sum fill what was padding). It hashes the key prefix as it is (SHA-256 output is already
  uniform) and is sized once at open from the packs' tail counts.
- The same experiment took 190 ms and 267 MiB, raw index buffers included. For today's 642 k entries it took 18 ms and
  35 MiB, against 62–112 ms and 180 MiB before.

**Safety of the compact index.** A hit reads the payload. It then computes SHA-256 of the lookup's *whole* key and the
payload, and compares the first 12 bytes with the stored sum.
- A lookup whose key shares only the prefix with a stored key would return the wrong payload. Its sum differs, so it is
  a miss, except with probability 2^-96.
- Hitting that case needs a prefix collision first, which happens with probability about n/2^64 per lookup (n is the
  number of entries). So a wrong answer has probability about n·2^-160 per lookup, about 2^-137 at 6 M entries. A guest
  chooses the request bodies, but each candidate costs a real fexc compile and the payload is always a real fexc
  answer: a 160-bit joint condition is out of reach.
- A damaged payload goes undetected with probability 2^-96, where the full 32-byte sum made it 2^-256.
- This sum check is the only guard against a wrong key's answer. Admission validates the object (that it is safe to
  run), not that it was compiled from these guest bytes.
- Two stored keys sharing a prefix (about n²/2^65, about 10^-6 at 6 M entries) leave one of them missing for good: it
  is compiled again each session.
- The exact alternative was weighed and not taken: the slot would keep the pack and record number, and a hit would
  read the 76-byte record to compare the full key and sum. That costs a second `pread` per hit (about 1 µs, against a
  hit's measured 4.2 µs; about 35 ms over a Konsole start's 35k lookups) to remove a risk far below hardware error
  rates. Both reviewers agreed.

**Descriptors.** Each pack a process reads holds one open descriptor for the whole run. At 4 GiB that is up to about
1,300 descriptors, and at an 8 GiB guest's 8 GiB about 2,600 (packs average 3.2 MiB locally, because a pack is written every 2 s as well as at 4 MiB); at the old
default it was about 120. natemu does not raise its own limit, and macOS's default soft limit is 256.
- Before this change, a pack of this build that failed `File::open` was deleted as damaged, so running out of
  descriptors deleted good packs. Without a bound, a large cache would also take every descriptor the rest of the boot
  needs (the vCPUs' compiler channels fail with `shm_open: Too many open files`).
- Now a process opens its packs most recently used first, at most half its soft descriptor limit
  (`descriptor_budget`). A pack past that budget, or past an `EMFILE`/`ENFILE`, stays on disk, counted against the cap
  but not read. It is among the least recently used, so it goes first at a later open.
- A pack another process removed meanwhile (`ENOENT`) is not counted. A pack that fails to open for any other reason
  (its permissions, say) can never hit, so it is deleted.

## Alternatives considered

- **Two-hit admission** (store an entry only when a second session compiles it). This would keep code seen in one
  session only out of the cache. But a desktop's second session is exactly the case that needs the first session's
  entries: the gain from a two-hit filter would only arrive in the third session. Last-use eviction already drops code
  seen once before code seen again, without that delay. Not done.
- **Per-session generations** (a session's packs as one unit, evicted together). Eviction is already by pack in time
  order, and the packs one run writes are consecutive. Last-use stamps give generations' effect without a new
  structure.
- **Sizing from a measured working set.** The factor is set from the KDE numbers above. A per-machine adaptive cap
  (grow while hits keep coming from the oldest packs) makes the cache's behaviour depend on history and is hard to
  test. A factor of the guest's RAM with a free-space bound is enough for now; the factor is provisional until warm
  sessions' hit rate against the cap is measured (below).
- **Memory-mapped packs** (`mmap` instead of `pread` on a hit). This saves a syscall per hit (about 1 µs; the payload
  is copied into a `Vec` either way for admission). Its larger benefit, which the design reviewer pointed out, is
  that a pack mapped and closed holds no descriptor, which removes the descriptor budget's reason to exist. The risk is
  SIGBUS instead of a miss if something outside natemu truncates a pack in place; natemu never does, and unlinking
  keeps a mapping valid. Not done here: the budget is the smaller change. It is the first follow-up if the
  measurement shows packs left unread.
- **Fewer, larger packs.** A pack is written every 2 s as well as at 4 MiB, so a warm session's trickle of misses
  writes many small packs (locally 37 of 145 under 1 MiB, 23 under 64 KiB). Oldest-first eviction at 384 MiB used to
  clear them; last-use eviction at 3 GiB keeps them. Descriptors and open time grow with the pack count. A follow-up
  if the measurement shows many: write on age only above about 64 KiB, or merge small packs at open.
- **Partly hot packs** pin their cold entries: one hit keeps a whole pack. Packs written during a warm session mix
  every running program's misses, so the claim that a pack's hot and cold entries are mostly one program's is
  unmeasured. A follow-up if the cache fills with stamped packs: log the bytes hit against each stamped pack's bytes
  at exit, and rewrite the hit entries of packs about to be evicted.
- **A sorted vector index** (24-byte entries, interpolation search over uniform keys) would halve the index's memory
  (about 140 MiB at 5.8 M entries, against 277 MiB), and a persisted side index could be mapped instead of built.
  Not needed for this change.
- **Per-entry LRU or compaction** (rewrite hit entries into new packs, drop cold ones). This gives finer grain than per
  pack, at the cost of rewriting data on every session. Pack-level stamps keep whole packs that are only partly hot.
  Packs group code compiled at the same time (the same program's start), so the hot and cold entries in a pack are
  mostly the same program's.
- **A fixed 4 GiB default.** Simpler, but a nearly full disk would give the cache 4 GiB anyway. The writer survives a
  full disk (the entries are lost, not the run), but the user's disk is still full.

## Tests

The unit tests are in `compile_cache.rs`: `default_cap_bounds`, `config_cap`, `hit_packs_outlive_newer_untouched_ones`
(which also checks that a second hit in a pack sends no second stamp), `unreadable_pack_goes`,
`packs_past_the_descriptor_budget_are_kept_unread` and `a_key_sharing_the_index_prefix_misses`. Each was shown to fail
against a mutant of the code it checks. The framework selection is `tools/natemu-test run tests/unit` (suite `rust`), and
`tools/natemu-test run` for the affected tier.

## Measurement

There is no separate measurement for this change; the owner keeps serious measurements to checkpoints. It goes into a
pooled checkpoint run with P1 step 1 and fault-around, and the counters are read from that run:
- `disk_open` (the open's index build) and the pack count against the process's descriptor limit;
- disk hits against fexc compiles for the warm sessions' first app starts;
- `dropped_disk_hits` (the in-memory map's drops that came back from disk).

Warm-session runs that compare the hit rate against the cap will set the final factor (`CAP_PER_GUEST_RAM`). Eviction
by last use is checked at an explicit small cap, `--compile-cache-mb 1024`:
- session 1;
- a session that starts other apps;
- session 1's apps again, which should still hit;
- the same three sessions with main's build at the same cap, for the comparison.

## Review

Two independent reviews of the design (Fable) and the code (Opus) found one blocking defect, now fixed:

- **Blocking, fixed:** descriptors. A large cache could take every descriptor at open, and the packs that failed were
  the most recently used. Now: the descriptor budget, newest first, and the error handling above.
- **Adopted:** 12 bytes of sum in the slot's padding (2^-96, was 2^-64); stamping again every hour; pack times ahead of
  the clock count as now; `ENOENT` packs are not counted and other unreadable ones go; a relative folder not made yet
  is measured at the working directory; the writer counts a pack's bytes only when it was written; the
  `unreadable_pack_goes` test skips under root (mode 0 does not stop root); `config_cap` accepts the cap of the free
  space before or after; the hit test asserts one stamp per pack; the measurement plan's eviction step at an explicit
  small cap, the cold `disk_open`, the pack count; this note's wording on admission, on the floor and on the steps.
- **Not adopted, with reasons above:** the exact second-`pread` check, two-hit admission, generations, an adaptive cap,
  memory-mapped packs, fewer and larger packs, rewriting partly hot packs, a sorted index. The last four are follow-ups
  that the Mac mini measurement decides.
- **Known, unchanged:** two calls of `Config::new` in the CLI's `old_command_lines_build_the_same_boot` test can in
  principle get caps a step apart, on a volume whose tenth of free space is below the guest's RAM and crosses a 2.56 GiB edge
  between them.

The default cap was then redone at the owner's request, as the guest's RAM, provisional (above). That change was
reviewed briefly as well (Opus).

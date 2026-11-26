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

**Default cap.** Without `--compile-cache-mb` or `engine.compile_cache_mb`, the cap is a tenth of the free space on the
cache folder's volume, at least 1 GiB and at most 4 GiB, rounded down to 256 MiB steps (`default_cap`). If statfs
fails, the cap is 4 GiB.
- *Floor, 1 GiB.* Each open keeps 3/4 of the cap, 768 MiB here, which holds a KDE session's 592 MiB.
- *Ceiling, 4 GiB.* Each open keeps 3 GiB, five times a session, which leaves room for its apps and a browser. Each run
  also has 1 GiB to write into, more than a cold session's 592 MiB. Half of the measured 8 GiB setting is enough
  because hits come from the code that sessions share. A browser's own working set has not been measured yet (see the
  measurement below).
- *A tenth of the free space.* This keeps a Mac with a small or nearly full disk from losing 4 GiB to the cache. Any
  volume with 40 GiB free gets the ceiling, which is a fixed value on most machines. The 256 MiB steps keep two calls
  made moments apart from disagreeing.
- The free space does not count what the cache itself already holds. So on a volume with less than 40 GiB free, the
  cap is a little smaller than a tenth of "free plus cache". Each open moves the cap by at most a step.

The knobs are unchanged: `--compile-cache-mb N`, `engine.compile_cache_mb` and `NATEMU_COMPILE_CACHE_MB` set the cap
exactly. The startup line `natemu: code cache: pool on, disk N MiB at DIR` shows the cap that was computed.

**Eviction by last use.** A pack's modification time now records its last use: when it was written, or when a process
last hit it.
- On a process's first hit in a pack (an `AtomicBool` per pack), the lookup sends the pack's number to the cache's
  writer thread. The writer thread then sets the pack's time to now (`File::set_modified`, on the read-only descriptor
  it already has).
- This costs one syscall per pack per process, and none of it is on the compile path.
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
- The new index keys an entry by the first 8 bytes of its key. It keeps the first 8 bytes of the entry's sum, with
  `u32` pack, offset and length: 33 bytes per slot. It hashes the key prefix as it is (SHA-256 output is already
  uniform) and is sized once at open from the packs' tail counts.
- The same experiment took 190 ms and 267 MiB, raw index buffers included. For today's 642 k entries it took 18 ms and
  35 MiB, against 62–112 ms and 180 MiB before.

**Safety of the compact index.** A hit reads the payload. It then computes SHA-256 of the lookup's *whole* key and the
payload, and compares the first 8 bytes with the stored sum.
- A lookup whose key shares only the prefix with a stored key would return the wrong payload. Its sum differs, so it is
  a miss, except with probability 2^-64.
- Hitting that case needs a prefix collision first, which happens with probability about n/2^64 per lookup (n is the
  number of entries). So a wrong answer has probability about n·2^-128 per lookup, about 2^-105 at 6 M entries.
- A damaged payload goes undetected with probability 2^-64, where the full 32-byte sum made it 2^-256.
- Every answer, hit or fresh, still goes through admission's validation (`Compiler`).
- **Open question for review:** the alternative is exact. The slot would keep the pack and the record number (8
  bytes, 17 bytes per table slot in all). A hit would then read the 76-byte record (full key, offset, length, full sum)
  and compare the full key and the full sum. That costs a second `pread` per hit, about 1 µs on a page-cached pack,
  against a hit's measured 4.2 µs. A Konsole start makes about 35k lookups.

**A pack that cannot be opened is kept.** Before this change, a pack of this build that failed `File::open` was deleted
as damaged. A cache of up to about 1,300 packs at 4 GiB can run out of descriptors at macOS's default soft limit of
256. That would have deleted good packs, though a natemu boot needs a higher limit anyway (testing.md).
- Such a pack is now left on disk and counted against the cap, but not indexed.
- It never gets a hit, so its time never moves. It is evicted as least recently used once it is the oldest. It does not
  stay forever, but it holds its share of the cap until then.
- An option, not done: delete it unless the error is EMFILE or ENFILE.

Each pack still holds one open descriptor for the whole run. At 4 GiB that is up to about 1,300 descriptors (packs
average 3.2 MiB locally, because a pack is written every 2 s as well as at 4 MiB).

## Alternatives considered

- **Two-hit admission** (store an entry only when a second session compiles it). This would keep code seen in one
  session only out of the cache. But a desktop's second session is exactly the case that needs the first session's
  entries: the gain from a two-hit filter would only arrive in the third session. The disk-write saving does not justify
  that. Not done.
- **Per-session generations** (a session's packs as one unit, evicted together). Eviction is already by pack in time
  order, and the packs one run writes are consecutive. Last-use stamps give generations' effect without a new
  structure.
- **Sizing from a measured working set.** The ceiling is set from the KDE numbers above. A per-machine adaptive cap
  (grow while hits keep coming from the oldest packs) makes the cache's behaviour depend on history and is hard to
  test. A fixed ceiling with a free-space bound is enough until the browser measurement says otherwise.
- **Memory-mapped packs** (`mmap` instead of `pread` on a hit). This saves a syscall and a copy per hit (the payload is
  copied into a `Vec` either way for admission). But a pack truncated by anything outside natemu would then raise
  SIGBUS instead of a miss. Packs are immutable and unlinking keeps a mapping valid, so the risk is small. The gain is
  about 1 µs per hit. Not done; worth reconsidering if hits show up in a profile.
- **Per-entry LRU or compaction** (rewrite hit entries into new packs, drop cold ones). This gives finer grain than per
  pack, at the cost of rewriting data on every session. Pack-level stamps keep whole packs that are only partly hot.
  Packs group code compiled at the same time (the same program's start), so the hot and cold entries in a pack are
  mostly the same program's.
- **A fixed 4 GiB default.** Simpler, but a nearly full disk would give the cache 4 GiB anyway. The writer survives a
  full disk (the entries are lost, not the run), but the user's disk is still full.

## Tests

The unit tests are in `compile_cache.rs`: `default_cap_bounds`, `config_cap`, `hit_packs_outlive_newer_untouched_ones`,
`unopenable_pack_is_kept` and `a_key_sharing_the_index_prefix_misses`. Each was shown to fail against a mutant of the
code it checks. The framework selection is `tools/natemu-test run tests/unit` (suite `rust`), and
`tools/natemu-test run` for the affected tier.

## Measurement for the Mac mini M4 (to schedule)

Run it on the KDE desktop, as the measurements above were, with the default cap (no `--compile-cache-mb`) and a fresh
cache folder:
1. Session 1: KDE, then first starts of Konsole and Firefox. Record the startup line's cap, and the cache's bytes and
   pack count at the end.
2. Session 2: the same. Record disk hits against fexc compiles for each first app start, the start times against the
   8 GiB runs, and the time from the perf log's `disk_open` mark (the open's index build) with the host's peak RSS.
3. Session 3, after a session 2' that started other apps: check that Konsole and Firefox still hit 98–99%. This is what
   eviction by last use is for.

The framework's `speedometer-natemu-warm` case records the compile cache's bytes after its fill boot. That gives
Firefox's own working set (it sets an 8 GiB cap of its own, so this change does not move it).

# Reusing another vCPU's translations inside the VM: design

Status: accepted after independent review (two rounds), November 13, 2026, on `perf-real-workload` at 0a814e4; design only,
no code (findings and fixes in the review response at the end). Inputs: the IO5 profile
(`natemu-raw/io5-round-20261113-01/c1-a624ef4-r1/perf-configure.log` and `perf-make.log`, the `vcpu` lines),
[pic-plan.md](pic-plan.md), [perf-plan.md](perf-plan.md) chunks 3, 4, 10 and 13, `crates/runtime/src/cache.rs`,
`crates/runtime/src/execution.rs` (`reused`, `ask`, `mark`), `crates/abi/src/execution.rs` (area layout),
`crates/host/src/execution.rs` (`Answers`, `prepare`'s spans, `Compiler::gating`). Estimates are labelled and show their
arithmetic.

## Recommendation in one paragraph

Build a shared, host-filled pool of admitted template translations, mapped read-only into every vCPU's resident and
looked up by content exactly as the host's answer cache is: (context, RIP half and page offset, leading bytes), then the
answer's bytes compared with the guest's. On a miss in its own cache and hint table, the resident probes the pool before
asking the host; a hit is copied into its own code area and relocated by the same re-check that same-vCPU reuse already
uses (perf chunk 10's `relocate_code`), so placement, admission's guarantee and invalidation are unchanged in kind. The pool
does not depend on any peer's code area, records or flushes. Mapping peers' code areas (the other option) couples every
vCPU to every peer's private, non-atomic bookkeeping and costs about 0.5 GiB of address space per peer. Estimated gain:
3-6% of configure's vCPU time and 1.5-2.7% of `make -j4`'s, from removing about 2-3 µs from each of 2-6 M host round
trips. A much smaller host-only change, skipping admission again on host hits, is option (d): estimated to save about 3.3 s on
configure. The first chunk counts what each option would have saved, and the counts choose between them.

## 1. What the profile says

Totals over the four vCPUs (vCPU thread CPU time; resident buckets; host `parts`):

| | configure | make -j4 |
| --- | --- | --- |
| vCPU CPU time | 119.5 s | 665.8 s |
| host requests | 3.37 M | 7.90 M |
| compiled by fexc | 0.60 M (fexc 4.7 s) | 1.12 M (fexc 13.8 s) |
| host memory hits | 3.16 M | 7.01 M |
| of them the asking vCPU's own answers | 0.61 M (19%) | 1.41 M (20%) |
| of them another vCPU's | 2.55 M (81%) | 5.60 M (80%) |
| of the hits, relocated by the host to another RIP (`relocated`) | 1.36 M | 1.53 M |
| resident `host` bucket (calls) | 21.6 s (4.34 M) | 51.3 s (8.54 M) |
| of it admission | 3.6 s | 8.8 s |
| in-VM copies and instances by same-vCPU reuse (`k1.reuse.copies`) | 9.22 M (0.21 µs each) | 7.50 M (0.25 µs each) |

Correction to the brief: `state.relocated` (1.36 M) counts host hits relocated to another RIP, not reuse inside the VM;
same-vCPU reuse inside the VM is the 9.22 M above. So the hits split into 1.80 M at the same RIP (kernel code, forked
children, fixed-address programs) and 1.36 M at another RIP (ASLR).

Cost of a host hit (estimate): the `host` bucket less fexc, the disk-cache put and the timing log's own writing (host
`log`: 3.02 s and 6.42 s, present only in profile runs), per host call: (21.6 - 4.7 - 1.05 - 3.0) s / 4.34 M = 2.9 µs on
configure; (51.3 - 13.8 - 2.4 - 6.4) / 8.54 M = 3.4 µs on make. This holds the exit and entry, lookup, relocation,
admission (about 1.06 µs: 3.57 s over 3.37 M requests), staging and the resident's reply check and placement.

## 2. Options

**(a) Peers' code areas mapped read-only.** Each vCPU maps every peer's code area, metadata arena and lookup area
(128 + 256 + about 95 MiB) at new addresses, probes each peer's hint table by (frame, page offset, context), and copies a
peer's live template as `cache::copy` copies its own.

- Address space: about 480 MiB per peer. The per-vCPU part of the private window has about 0.9 GiB free (128 MiB at
  `PRIVATE + 0x1800_0000`, and from the global area's end, about `PRIVATE + 0x4D60_0000`, to guest RAM at 2 GiB), so two
  peers fit and three do not; 64 vCPUs need about 30 GiB and a new window layout. Page tables grow with n².
- Concurrency: a peer's records, hints, `code_used` and link words are written by that peer alone with plain stores. Reading
  them needs a per-vCPU flush sequence counter in the global area, release ordering on every filing, and restoring link and
  `svc` words patched while being copied. A peer's capacity flush rewrites its area under the reader.
- Currency: a peer's block "live" does not mean its bytes are current: a write posted to the peer and not yet drained
  leaves the block live. The reader would have to read the peer's pending `stale` bits after its own mark, and rely on the
  peer setting `DEAD` before clearing a stale bit. Records hold no guest bytes, so the reader cannot compare bytes instead.
- Coverage: only blocks some peer holds live now; lost at each peer's flush; only the same frame.
- Lookup: n - 1 probes per miss.

**(b) A shared pool of admitted templates, filled by the host (recommended).** The host already keeps every answer and
already admits it at the asking RIP. After admitting an answer that is a template (the host's `relocatable` bit: it
depends only on bytes of the block's own pages, nothing cut it), the host also writes it, in the form `place` keeps a
template (code with link points and the dispatcher placeholder as admitted, sites, packed relocations, link points,
exits), with the bytes it was compiled from, into the pool. Residents only read the pool.

- Address space: one region shared by all vCPUs, like the global area: about 272 MiB at `PRIVATE + 0x6000_0000`, inside
  the free room below guest RAM. Constant in the number of vCPUs.
- Concurrency: one writer (the host, under one lock), many readers; a ring with a published tail (section 3). No reader
  ever depends on a peer.
- Currency: by content, as the host's cache: an entry matches only if the guest's bytes now equal the bytes the answer
  read. Stale entries are never invalidated; they simply stop matching.
- Coverage: every template the host has published and not overwritten, whoever compiled it, at any frame with those bytes
  (configure's thousands of freshly linked `conftest` programs share startup code but not frames).

**(c) One code area shared by all vCPUs** (the investigation's backlog item). Removes the per-vCPU copy too, but links,
L1, dispatcher slots and flushes are per vCPU today, and freeing code another vCPU may be running needs stop-the-world or
epoch reclamation. A redesign, not a step; (b) does not preclude it.

**(d) Host only: keep admitted answers and skip admission on hits.** The host keeps the admitted form with each answer,
with the request's CPL-0 and bulk values admission ran under. Admission depends on more than the bytes and the RIP: it
takes the request's CPL 0 (`admission.rs` 232-238) and outside CPL 0 refuses RDTSC, SWAPGS, CLI/STI, VERW (310-343) and
CR2 field loads (host `execution.rs` 4048-4050); the host refuses a bulk object for a non-bulk request (2383); and the
memory cache matches only the gating values fexc says it read (1685-1687), so admission's own checks must not be skipped
on fexc's word. So a kept admitted form is reused only when the hit's CPL 0 and bulk values equal those kept and the kept
prefix covers at least the block's guest bytes: as is at the same RIP; at another RIP only when every relocation packs
(at most `MAX_RELOCS`) and `relocate_code` accepts (the re-check chunk 10 accepted inside the resident). Anything else is
admitted as today. Saves the admission of each hit (estimate, at the mean per request): 1.06 µs × 3.16 M
= 3.3 s on configure and 1.12 µs × 7.01 M = 7.8 s on make, with the round trip kept. A peer of (b), not only its fallback: it is a small change, it
also serves the hits the pool misses, and chunk 1 counts both.

## 3. Design of (b)

**Entry and key.** An entry is a header (the RIP it was admitted at, context, code size, guest bytes, site and relocation
counts, flags `RELOCATABLE` and `STI_FIRST`, link points, computed-exit and REP-exit offsets, prefix length) followed by
the prefix bytes, the code, the sites and the packed, sorted relocations. The format and both the writer's encoding and
the reader's decoding live in `natemu_abi` (as `relocate_code` does), so host and resident share one implementation and the
host's unit tests cover the reader. Key: hash of (context, `rip & (1 << 63 | 0xfff)`, the first L prefix bytes with
L = min(4, prefix length)), as the host's `key` (block limit: only answers with the run's default limit and no cut are
templates). The ring is 256 MiB; every entry starts at a 64-byte boundary and is at most `STAGING_BYTES` (128 KiB; the
largest block is 32 KiB of code plus its tables). A position is a monotonic byte count of everything ever published; an
entry's place in the ring is its position modulo the ring size. An entry never straddles the ring's end: when it would,
the writer skips to the next lap (the skipped bytes are never named by a slot). The index is 2^21 u64 slots in 4-way sets,
each {position / 64 (40 bits, 64 TiB of publication), 24-bit tag}: a slot names a full position, never only an offset in
the ring, so a slot left over from an earlier lap cannot point into the middle of a newer entry (review finding B1).
Publication starts at position 64, so a zero slot is empty.
Prefixes shorter than four bytes (a lone `ret`) need probes at each length, longest first, as the host's `Shard::find`
does: at most four probes.

**Gating values.** The host matches on fexc's gating values, the resident on the context. For non-step block requests the
gating values are functions of state the context encodes (`execution.rs` 3234-3241: SSE/AVX/x87 gating, bit-test cut,
CPL 0, the clock constant); same-vCPU reuse rests on the same fact. CPL 0 and bulk, which option (d) must match, are
fixed by the context here: it holds the CPL and the alignment (bit-test) cut (`natemu_abi` 1877-1884), and bulk is the negation of
that cut for non-step requests, so (b) needs no extra key. The host asserts it on every block request that reaches it
(requests the pool serves are checked only under the check mode, which asks the host for each): per
context, the first gating vector seen. A request whose vector differs turns the pool off for the run (a word in the pool's
header that the host clears and every lookup reads first; entries already published are then never used), is counted, and
under the check mode is a host error. Expected never. CPU features are one value per run: the pool exists only when every
vCPU's compiler shares the run's answer map (`Compiler::share`).

**Publication (host, one `Mutex`).** An answer is published once: the host keeps the position it last published it at and
publishes it again only when that position has fallen below the tail. To append an entry of n bytes at position p (after
any skip to the next lap): store tail = p + n - ring size, saturating at 0 before the first lap (relaxed), then a release fence (`dmb ish`), then the entry's
bytes and header (plain stores), then the index slot (release store). The fence orders the tail before every later data
store; a plain release store of the tail would not (review finding B2). Readers never write.

**Lookup (resident, in `reused`, after its own hint table finds nothing and before `ask`).** The walk and mark of RIP's
page are already done (`fetch_first`); `mark` has already shot down any peer's writable shadow of the page, so bytes read
now stay as read until a write that is posted to this vCPU. Then:

1. Refuse where `reused` refuses (instruction breakpoints armed, the stop RIP or a host fence in the bytes or on RIP's
   page, hooks beyond four, pool off).
2. Read the lead bytes through the guest-RAM alias at the walked frame; probe; for a tag match, take the position P from
   the slot (acquire load), copy the entry at P's place into the staging area (private, unused between requests): its
   header first, then as many bytes as its length says, refused if the length exceeds `STAGING_BYTES` or the ring's end.
   Then `dmb ishld`, then load the tail. Accept only if the copied header's position equals P and P is at or above that
   tail: then every byte copied was written by the host as part of the entry it published at P, and none was overwritten
   during the copy. All checks below read the private copy, so a later overwrite cannot change what was checked.
3. Decode the copy with bounds on every count and offset: code size at most the largest block, sites, relocations (at most
   `MAX_RELOCS`) and link points inside the entry's length, computed-exit and REP-exit offsets inside the code, the prefix
   length at least the block's guest bytes. Compare context and half/offset; if the prefix runs into the next page, walk and
   mark it (as `reused` does for a crossing template). Compare the prefix with the guest's bytes at the walked frames.
4. Take invalidations; refuse if the generation moved (as `reused`).
5. Run the reply checks of `ask` on the copy (sizes, link points as admitted, the dispatcher placeholder), then place it
   through `cache::copy` generalised to take its source from the staged entry instead of its own code area: copy the code,
   `relocate_code` from the entry's RIP to RIP, relocate the computed exit to this vCPU's dispatcher slot, file it with its
   frames, linear pages, L2 and hint. It is then an ordinary template of this vCPU, which the lazy PIC rewrite may share.
6. Any refusal resets the walk state as `reused` does and asks the host. Under `REUSE_CHECK` every pool placement is
   also asked of the host and compared (`checked`, unchanged).

**Correctness.**

- Bytes: the entry is the host's answer for its prefix bytes under its context; the resident read the same bytes after
  marking the frames, as the host reads them after the same mark on a request. A write before the mark is in the bytes
  read (shadows shot down); a write after it is posted here and kills the block (step 4 or the next drain). SMC on a page
  another vCPU translated is therefore handled by the content compare, not by that vCPU's state.
- Admission: the entry is the object admitted at its RIP r0, before any link or relocation of the dispatcher. Moving it to
  r is exactly chunk 10's re-check: same half and page offset, canonical RIP and block end, relocations in order, each move
  re-encoded by FEX's rules with the same word count, each literal moved and canonical, each link point a link point after
  the move. Every other admission rule reads offsets, bytes or values that move with r - r0 (chunk 3's proof; chunk 10's
  argument), and the check mode has compared millions of such copies with the host's answers (chunk 10: 850 k at N = 1,
  0 mismatches). The pool adds no new relocation rule, so admission's guarantee carries over; only the source of the
  template changes. Non-template answers are never published.
- Lifetime: entries are never freed under a reader; an overwrite is detected by the tail (step 2). Peers' flushes and
  deaths do not matter: the pool is not their code.
- Memory ordering (step 2 against publication): if any byte the reader copied came from a later entry, the writer's tail
  store precedes that byte's store (its release fence), and the reader's tail load follows the copy (its `dmb ishld`), so
  the reader sees the later tail and refuses. A slot read with acquire names an entry whose bytes are all visible.
- Containment: the pool is mapped only at EL1, read-only (a new read-only data right in `memory.rs`; `READ_ONLY` exists),
  never in a guest shadow. Nothing in the resident admits code again: neither the reply checks of `ask` nor `relocate_code`
  would catch Arm code made of guest bytes. So the publication check of step 2 is what containment rests on: the resident
  accepts only a whole entry the host wrote at the position a slot names, after the host admitted it, as the host's own
  rule requires of every object it hands out (`execution.rs` 1925-1927). The decode bounds of step 3 keep a host defect
  from making the resident read or write outside its staging copy; with them, a stale slot or a lap overwrite is refused
  and goes to the host.

**Memory.** Index 16 MiB plus ring 256 MiB of shared backing, placed between the last vCPU's block and guest RAM, so RAM
stays the backing's last bytes as `prepare` assumes (`execution.rs` 295); RSS only as touched. Its address range gets a
compile-time check beside the area checks in `natemu_abi` (whose comment "every area lies in PRIVATE..PRIVATE + 1 GiB" is
stale since the global area moved above it; to be corrected then). The host's answer cache stays (it keeps non-templates
and refills the pool). Entry size (estimate, about 500 bytes: 170-260 of code, sites, relocations, prefix, 64 of header):
256 MiB holds about 0.5 M entries against 0.60 M distinct answers on configure and 1.12 M on make, so make will cycle the
ring; the counting chunk models the publication rule and sizes it.

## 4. Expected gain (estimates)

Served share f of the host's memory hits: unknown, assumed 0.6-0.9 (non-templates, ring overwrites, refusals); the
counting chunk replaces it. Pool hit cost: 0.5-1.0 µs (same-vCPU reuse 0.21-0.25 µs per copy plus 0.21-0.23 µs per
placement, measured; plus up to four probes and a ~500-byte copy from shared memory). Saving per served request:
2.9 - (0.5..1.0) = 1.9-2.4 µs on configure, 3.4 - (0.5..1.0) = 2.4-2.9 µs on make.

- configure: 3.16 M × (0.6..0.9) × (1.9..2.4) µs = 3.6-6.8 vCPU-s of 119.5 (3-5.7%). Configure runs about one process at
  a time, so wall should drop by about as many seconds (4-7% of 95 s).
- make -j4: 7.01 M × (0.6..0.9) × (2.4..2.9) µs = 10-18 vCPU-s of 666 (1.5-2.7%), about the same share of wall.
- Option (d) alone: 3.3 s and 7.8 s (section 2). With both, (d) applies to the hits the pool does not serve.
- Cost on misses: a request the pool does not serve now pays up to four probes into a 16 MiB index first (estimate
  0.1-0.3 µs); chunk 4 counts it.

Not fixed: first-ever compiles (0.60 M and 1.12 M; fexc 4.7 s and 13.8 s); each vCPU still copies, files and links every
block in its own area (link bucket 9.9 s and 70 s), per-process instances, L1 refills, revalidation, page faults and
shadows; non-template answers and system-instruction decodes still go to the host.

## 5. Chunks

**Chunk 1, counting (host only, no resident change).** At each request the host already knows whether its answer is a
template, its key, prefix and context. Simulate the pool: a map from key to ring position, a ring byte counter, entry
sizes as the encoder would write them, the publish-once rule. Count, per vCPU and run: requests the pool would have served
(by own/peer, same RIP/other RIP), at ring sizes 128, 256 and 512 MiB; the frame-keyed subset (would a hint-style key by
guest frame do); prefixes shorter than four bytes, and shorter than the block's guest bytes; templates vs not among hits;
gating vectors that differ for one context (must be 0); entry size histogram; and for option (d), hits whose kept prefix
covers the block's guest bytes, with the admission time they spent. A `pool {...}` line in the timing log. Run
`tools/run_io5.py profile` with configure and make; take the per-hit costs from a timing-only round where possible.
Gate for (b): served ≥ 1.8 M on configure (at 2.0 µs, 3% of 119.5 s), else (b) stops and is recorded. (d) is decided on
its own count; it can land before or without (b).

**Chunk 2, the pool and publication (abi, host; resident unchanged).** The `natemu_abi` pool module (format, writer,
reader with the slot-position and tail checks, decode bounds); the read-only span and backing; the address-range check;
publication after admission of templates, off unless enabled. Tests: encode/decode round trip; the decoder on arbitrary
bytes (an error or an entry within bounds, never a read outside the copy); a forged header placed inside an entry's prefix
bytes, with a slot naming its offset from an earlier lap: refused; writer and reader threads using the production fences,
a tiny ring and variable-size entries, so that slots from earlier laps land in the middle of newer entries during reads
(an accepted entry is never torn or forged: each entry carries a checksum in the test build only); the gating switch.
Measure publication cost on a configure profile (expected under 0.1 s per vCPU).

**Chunk 3, the resident lookup.** As section 3, `Control::pool` on/off for A/B, counters (probes, hits, refusals by
reason: no slot, key, bytes, overwritten, fenced, re-check), a `pool` perf bucket. The program driver gains one shared
answer map and pool for its vCPUs under an option. New K1 programs (natemu only, 1-4 vCPUs, both block sizes, the check
on and off), reusing the `reuse-*` and `pic-*` bodies:

- `pool-peer`: CPU 0 runs a frame at RV1, CPU 1 runs it at RV2 in its own table: no host request there, its own RIP-relative
  and return-address values; with a tiny code area so CPU 0 flushes in between.
- `pool-frame`: the same bytes in another frame: served, own values.
- `pool-smc`: CPU 1 rewrites a page CPU 0 translated, then CPU 2 runs it: new bytes' effects; and the write racing CPU 2's
  lookup after its mark (`pic-xwrite`'s handshake): the placed block dies, new bytes run.
- `pool-ctx`: the same bytes under another context (CR0.TS, an unmasked MXCSR exception, CPL): not served.
- `pool-cross`: a crossing block whose second page differs on the second CPU: not served.
- `pool-wrap`: a tiny ring overwritten while CPUs read: `overwritten` refusals > 0, values right.
- `pool-fence`: a breakpoint, a hook and the stop RIP on the page: not served.

Each red under its mutation (no bytes compare, no tail check, no slot-position check, no prefix-length check, no
context compare, no second-frame compare), as chunk 10's were; the slot-position mutation is red in `pool-wrap` with the
forged-header layout of chunk 2's test. A `--check-reuse` boot with 0 mismatches.

**Chunk 4, measurement.** `tools/run_io5.py profile`, configure and make, pool on against off, interleaved, three
timing-only rounds plus one perf-log round (VM lock taken). Count the misses' added probe time. Gate: configure vCPU time
down at least 3% and wall not up; make not slower beyond its spread; measured pool hit cost at most 1.0 µs. Then the
standing suite once before merge and the benchmark round at completion.

**Stop rule.** (b) stops after chunk 1 if it counts under 1.8 M served on configure. After chunk 4, if the gain is under
half of chunk 1's count times 2.0 µs, or make regresses, revert chunk 3 (keep the counters) and record why; option (d)
stands on its own count either way.

## Review response (November 13)

Independent review: accept with fixes; admission's carry-over and the coherence argument confirmed. All findings accepted.

- B1 (blocking): a slot held only an offset in the ring, so after a lap it could name the middle of a newer entry, whose
  guest-chosen prefix bytes could pass as a header; nothing in the resident admits code again, so containment would have
  failed. Fixed: slots hold the full position; the reader accepts only an entry whose header repeats it and which is not
  below the tail after the copy (section 3, step 2); the containment item now says the publication check is load-bearing;
  a forged-header unit test and a mutation are added.
- B2 (blocking): a release store of the tail does not order later data stores. Fixed: relaxed tail store, release fence,
  data, then the slot's release store. Also specified: entries at most `STAGING_BYTES`, never straddling the ring's end,
  and the reader's bounds on every count and offset.
- N1: the per-hit cost included the timing log's own writing; corrected to 2.9 and 3.4 µs, the gains re-estimated, and
  chunk 1's gate raised to 1.8 M.
- N2: option (d) is a peer of (b), counted in chunk 1.
- N3: the reader requires a prefix at least as long as the block's guest bytes; a gating mismatch turns the pool off.
- N4: publish once per answer; chunk 1 models it; make will cycle a 256 MiB ring.
- N5: the misses' added probes are counted in chunk 4.
- N6: tests added (forged header, arbitrary-bytes decoder, production fences with mid-entry slots, new mutations).
- N7: the pool goes before guest RAM in the backing, with an address-range check; the stale area comment is noted.

Second round: (b) accepted. One blocking finding in option (d): admission also depends on the request's CPL-0 and bulk
values and has checks independent of fexc's report, so (d) now keeps those two values with each admitted form and skips
admission only when they match, the prefix covers the guest bytes and, at another RIP, every relocation packs and
`relocate_code` accepts (section 2); section 3 says why (b) is unaffected. Minor: position 0 is reserved (a zero slot is
empty), the first lap's tail saturates at 0, and the gating assertion covers only requests that reach the host.

## Questions for the owner

1. Approve option (b): a host-filled pool, about 272 MiB of shared backing and address space below guest RAM?
2. Default on after chunk 4's gate, or behind a flag until the next benchmark round?

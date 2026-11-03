# Performance phase plan (draft)

Status: draft, November 3, 2026, HEAD 335c070 (series47-seg); revised after independent review. Scope: owner decisions of November 2/3 ([boot-features plan](boot-features-plan.md) §5): faster first-time
code, measured first; the correctness pass follows. Principles: measure first; native mechanisms; nothing keyed to
Linux (a KASLR boot must gain too); every object still admitted; checks green; no debt.

## 1. Starting point

From [boot-handoff.md](boot-handoff.md) §4 (Debian 6.12.107, 2 GiB, `nokaslr`, wall to the end-point line):

| Wall s | natemu warm | natemu cache off | QEMU multi | QEMU single |
| --- | --- | --- | --- | --- |
| Kernel only (root-fs panic), N = 1 | 2.28 | 4.15 | 1.97 | 1.95 |
| Kernel only, N = 4 | 2.31 | 5.70 | 1.70 | 1.86 |
| Installer ("Select a language"), N = 1 | 67.3 | 67.8 | 7.27 | 7.25 |
| Installer, N = 4 | 49.1 | 53.1 | 5.46 | 7.62 |

Known (plan, Built (P3)): cache off, the installer makes 1,142,555 compiles and waits 35.4 s of 68.0 s for fexc
(7a2f714); a first-time block costs about 35 us cold, 20 us on a disk hit, 4-6 us on a memory
hit, 2 us resident. The RIP is in every key, so user code at randomized addresses compiles again per process; with
`norandmaps`, 140 k compiles and 33.7 s (one cold run, old cache code). So about 28-33 s lies outside compiles and mostly
does not scale with them; a few seconds may (about 3 us of host cost per request fits, admission about 2 us per block).
Compute loops tie QEMU (P1 diagnosis).

## 2. First chunk: the breakdown

Per vCPU thread and for the process, wall time split into named parts that add up: installer at N = 1 and 4 (warm,
off), kernel only at N = 1 and 4 with and without `nokaslr`; three runs each. **Acceptance:** wall minus the named
parts is under 5% of wall for every thread and the process, or the table is not delivered and the gap is chased first.
The log's own cost is measured (on against off).

**Process phases** (a `mach_absolute_time` stamp each): process start; `Boot::prepare` done (image load, tables;
`linux.rs`); `DiskCache::open` done, eviction included (`compile_cache.rs:188`); each `Server::start`
(`execution.rs:542`); each vCPU's first entry; the end point; VM destroyed (`linux.rs:213`); exit. Every vCPU's
virtual timer offset is 0 (`vm.rs:330`), so guest CNTVCT stamps are host `mach_absolute_time` stamps.

**Host log** (`NATEMU_PERF_LOG=FILE`, off by default, as `NATEMU_EXIT_LOG`; buffered per vCPU thread):

- In `serve` (`execution.rs:1133`; `run_until_exit` at 1153): the interval around each `hv_vcpu_run` and the host time
  to the next entry, per exit kind (`state.request`, 1181-1235). Each vCPU thread's CPU time (`thread_info`,
  `THREAD_BASIC_INFO`) beside its wall: wall without CPU time is preemption, which CNTVCT also counts.
- One line per translation request (`translation`, 1254): vCPU, CR3, RIP, result (memory hit, disk hit, compile,
  decode, next page, fetch fault), size, a hash of the bytes to the extent, and nanoseconds of fetch copy (1284),
  memory lookup (835), disk get with SHA-256 (930), fexc round trip (942), admission (860, run on memory hits too),
  disk put (955), staging (1332). Post-processing bounds reuse: bytes compiled again at another RIP with the same page
  offset; keys compiled on two vCPUs.
- fexc (`fexc.cpp:907`), with an environment variable: per request, the times of request read, compile done
  (`CompileRequest`, 372) and reply written; IPC = round trip minus these. At the end the host closes each server's
  stdin and reads its CPU time with `wait4` before `Server::drop` kills it (626-629).

**Resident counters** (`State`, `crates/abi/src/execution.rs`; in the `boot-linux` JSON, `drivers/linux.rs:205`), ticks
and counts of: hvc return to next hvc (`runtime/src/execution.rs:478`; the host's interval minus it is HVF entry and
exit); placement (`cache::place` at 194, `cache.rs:347`, cache maintenance 509-515); resident entries by kind, from
`returned` (764: link, miss, division, helper, stop, abort, IRQ, system instruction) to `enter_allocated` (757);
address-space work (handoff §4: about 4.4 us per CR3 load with its shadow reset): CR3 loads, shadow resets and
shadow-miss retries, revalidation walks, #PF deliveries, INVLPGs (`k1_stats` plus shadow-miss ticks); the HLT park
(`execution/events.rs:182-215`); waits on peers. Guest execution = in-VM resident-side time minus resident, halted and
waiting ticks, checked against thread CPU time. If the reads move `run_k2.py cost` beyond noise, a host-set flag gates
them.

**Per process.** CPU time of `natemu-host`, its vCPU threads and each fexc child; the macOS `sample` profiler on both
for 30 s of user space; in the guest, the log's CR3 groups compiles by address space (no guest structures read).
Guest-RIP sampling only if guest execution exceeds a quarter of wall at either N. Gate: the owner sees the tables and
ranked candidates before any optimisation, and sets the phase's target.

## 3. Candidate chunks

Each ships only if wall improves beyond the spread of three runs with nothing slower, and ends with the standing suite
at both RAM sizes and the boots of §5.

**Translations reusable across load addresses (host).** *Hypothesis:* most compiles are the same bytes at another
page-aligned base (shared libraries, PIE, a KASLR kernel). *RIP dependence today:* the memory key `(rip, limit)`
(`execution.rs:723`, 834); the disk key, SHA-256 of the whole body with `--rip` and the bytes to the page end
(`compile_cache.rs:182`, `execution.rs:929-930`); in the object, `guest_rip`, literal slots (block RIP, end, branch
target) and RIP moves (return addresses, RIP-relative operands). Split and alignment stubs of RIP-addressed accesses
depend only on the low 12 bits (`admission/structure.rs:874-915`). *Mechanism:* disk key = the body with the RIP
replaced by its page offset and half (kernel, user); memory key = page offset, half, limit and a hash of the leading
bytes, then `Entry::matches` (695). A hit at another RIP rewrites each relocation value and passes admission at the real
RIP as a fresh object does (`admission.rs:253`, 354-376; `structure.rs:594`). A relocated object admission refuses (a
value needing more move words, a non-canonical target) falls back to a compile, counted, logged and tamper-tested; a
fresh object's refusal stays a host error. **No-code answers** are reused only where the host reads no absolute RIP
(decodes from the bytes, `instruction_too_long`, `partial_decode`, `end_of_input`); `unsupported_form`, read through
`system::branch(bytes, rip)` (`execution.rs:891-895`), is compiled at the real RIP. *Correctness risk:* a RIP-derived
constant FEX folds without a relocation is invisible to admission. First a proof: replay a logged request set through
fexc at RIP and RIP + k * 4 KiB (several k, each half), requiring objects equal but for relocation values; each
difference is a fexc patch before reuse ships. Under the suites' code-shape option each reused answer is also compiled
fresh and compared. *Expected:* about 1 M compiles become hits that still cost a host round trip (4-6 us): roughly 25
of the 35 s. KASLR: the decompressor's relocations change bytes holding absolute addresses; the kernel's gain is
measured. *Decides:* reuse bound, proof differences, fallback rate.

**Reuse inside the resident.** *Hypothesis:* about 1 M exits then remain because the resident looks up by linear RIP
(plan, Built (K1)) though the physical page is translated at another base. *Mechanism:* on a miss the reverse map
(`PAGE_BLOCKS`) finds a block of that physical page, offset and context; with the relocation list admission gave it,
the resident copies it, applies the shift, checks canonicality and move fit, else asks the host. Only immediates of
words admission classified as RIP moves or literal slots change: no branch, exit or access is created. This is
admission-equivalent code and gets its own review. *Expected:* up to 1 M x 4-6 us. *Decides:* exits left after host
reuse.

**Shared in-memory cache and compiler pool.** *Hypothesis:* at N > 1 vCPUs compile the same blocks again; the cache
resets wholesale at 65,536 entries (`execution.rs:842-846`). *Mechanism:* one cache per VM (sharded locks), keyed on
`supported` too, holding the admitted form so a hit skips re-admission (every object still admitted for its RIP);
second-chance eviction instead of the reset; the `fexc --serve` children (536) become a pool. *Risk:* the tamper hook
(862) must act on a copy. *Decides:* duplicate-key share, admission time.

**Translating ahead of use.** After admission, queue the block's link targets (1339) on pages this request fetched (no
host walk of guest tables) to idle pool servers; answers enter the shared cache keyed by bytes read, used only if the
resident's bytes match (695). Worst case a wasted compile; bounded queue; host cores are the risk. *Decides:* share of
requests at a predecessor's target on a fetched page, then the speculative hit rate.

**Placing successors in the resident ahead of use.** A translate reply may carry admitted successors on pages the
resident marked for this request; it places each under the block's race rule (`kept`: a write since the mark kills
it). A first visit then costs a resident lookup (about 2 us), not a round trip. *Decides:* exits per first-time block.

**Address-space switches.** *Hypothesis:* CR3 loads and the shadow misses after them weigh heavily in a fork- and
exec-heavy installer. *Mechanism:* keep a few data-shadow sets tagged by CR3 root, chosen at a load instead of reset
(no PCID offered to the guest). A CR3 load without PCID flushes non-global entries, so a kept shadow must equal a fresh
walk: valid only while no page-table page on its walk was written, tracked as K1 tracks code pages (an A bit cleared
included). *Risk:* TLB semantics; new SMP cases in `run_k1.py`. *Decides:* CR3-load and shadow-miss ticks.

**Per-block overhead.** As the profile shows: fexc's fixed cost (Config rebuilt per request since P3's leak fix,
`fexc.cpp:463`; a JSON report the host drops for objects, `execution.rs:646`); IPC by pipes and `poll` (564-611), a
shared-memory ring with a native wait only if material; staging, placement; admission's quadratic site scans.

**Resident entry cost.** About 0.36 us fixed per entry, 0.167 us the cheapest (plan, P2 row). Ticks in `bank`
(`runtime/src/execution.rs:1502`), `sse::bank` (`sse.rs:28`), `block_at`, `enter` (715-757), `tables::select`
(`tables.rs:67`); then copy only what changed, skip `tables::select` when CPL and root are unchanged, link exits into
kept system records.

**Uncached RAM.** About 4.5 us per UC access through the step block; the boot's `cache_disable` window is about 533
accesses (2.5 ms). If `uncached_steps` makes it material: keep step translations in the resident.

**Quick PIT calibration.** First-time blocks inside its 24 us windows make it fail, warm runs too (memory hits 4-6 us;
plan, Built (P3)). Only resident placement ahead of use or reuse inside the resident brings its blocks to resident
cost; measured with `run_l4b.py`'s `quick_pit_calibrate` sequence cold, against QEMU.

## 4. Order and gates

1. The breakdown, accepted by its rule. Nothing else starts before it.
2. The relocation proof (fexc replay, no VM), in parallel with:
3. Shared cache with kept admission (small, low risk).
4. Host reuse across addresses, if the proof is clean.
5. Breakdown again; then reuse inside the resident, translating and placing ahead of use.
6. Address-space switches, per-block overhead, resident entry cost, uncached RAM: only where the latest breakdown bounds
   the saving at 5% or more of a boot's wall.

If the unexplained 28-33 s is guest execution, halted time or address-space work, its chunk goes before 5. Each chunk
has an independent reviewer.

## 5. Comparison with QEMU and records

Same end points, harness and QEMU command as the handoff §4 (wall from process start to the end-point line, serial
polled every 50 ms; guest printk time); QEMU 11.1.2 `thread=single` and `thread=multi`; N = 1 and 4 (2, 3 for kernel
only); both emulators with and without `nokaslr`; natemu cache warm and off; three runs, one VM at a time. Raw data in
`natemu-raw/perf-YYYYMMDD-NN/` (`boots.jsonl`, serial and perf logs, profiles, commit, fexc SHA-256). Each chunk's
"Built" note holds before/after tables; the handoff's boot tables and INV-03-01's "Next phase" row are updated. Claims
beyond development results get pre-execution review and replication.

## 6. Non-goals

AES-based RNG inside the VM; guest RAM beyond 3 GiB, more than 4 vCPUs; the correctness pass (differential fuzzing,
real workloads, hardware checks); guest-visible PCID, LA57, TSC-deadline, 1 GiB pages; x87 precision; FEX
code-generation tuning unless guest execution dominates; `ret` through the dispatcher and peer-store stepping unless the
breakdown shows them; the public API surface; anything keyed to Linux; skipping or weakening admission.

# I/O benchmark record

November 8-9, 2026. This is the measured I/O benchmark record: the one home for every measured
number in the copy-first I/O work, followed by the original tool-selection rationale (the tools
chosen and why, near the end of the file). It supplements [the I/O plan](io-plan.md);
[Track05](../research/tracks/05-benchmarking-and-validation.md) still owns comparison validity
and QEMU TCG remains the required baseline. Review verdicts live in the [findings
ledger](../research/reviews/INV-03-01-findings.md) and current status in
[INV-03-01](../research/investigations/INV-03-01.md#current-direction--build-first-october-25-2026).
The measured sections come first. Tool capabilities in the later selection sections are
source-backed, but their workload sizes and controls are recommendations, and the original
matrix and next-step plan are kept as historical planning, superseded by the measured sections.
Results are scoped to the stated workloads, not a claim of product-wide coverage.

**Implementation follow-up:** signed-index-verified fio 3.39-1 and its dependency closure are
staged outside the repository; eleven finite profiles have static review. The first
installed-root admission attempt timed out during its 64 MiB CRC workload. Following reviewed
targeted DMA invalidation changes, the unchanged original workload passes primary and
independent write/relaunch pairs, including all profile/library checks, CRC readback, persisted
hashes, clean S5 and fsck. This establishes functional tool admission, not a performance score
or causal speedup. The retained preparation, failures, diagnostics and passing pairs are at
`natemu-raw/io-benchmark-prep-20261108-01` and `natemu-raw/io-fio-admission-20261108-01`. All
seven bounded launches are accounted for; no additional run is assigned. Stock QEMU's
segment/size feature profile and fdatasync/fsync flush path differ from natemu's current
profile/F_FULLFSYNC: use it as a disclosed whole-system baseline, not an exact device or
durability match. No network benchmark endpoint has been contacted.

The network clients are now staged separately at
`natemu-raw/io-network-tools-20261108-01`: iperf3 `3.18-2+deb13u2`, curl
`8.14.1-2+deb13u5` and netperf `2.7.0+git20210121.3bc455b+ds-3`, with OpenSSL
and a bundle of 150 packaged CA roots. Independent static review verifies the
signed package index, all 66 archives/dependency edges/extracted files and the
41-object ELF dependency set. No guest tool or packet service has executed;
runtime options, DNS/TLS, network behavior and scores remain unqualified.
The preparation occupies about 85 MiB. The host has iperf3 but no netperf/netserver
on PATH; peer provisioning remains part of the later controlled measurement setup.

## First measured native and QEMU baselines — November 8

The owner's request to run the missing baselines is now executed under
`natemu-raw/io-fio-baselines-20261108-01`; `summary.json` binds the raw fio JSON.
All ten phases pass exact-byte/CRC/persistence checks. Guest pairs also pass
clean S5, offline fsck and byte-identical whole-image read-only restart. QEMU and
native results have independent replay, including a fresh native source build;
natemu has one fresh pair in this session, in addition to the earlier admission.

| Backend | 64 MiB write + CRC readback, fio job seconds | Separate 64 MiB verification read, fio job seconds |
| --- | ---: | ---: |
| Native arm64 macOS fio 3.39 | 0.220–0.221 | 0.042–0.045 |
| QEMU 11.1.2 TCG, two vCPUs | 1.898–2.041 | 0.823–0.831 |
| Natemu before the performance repair, two vCPUs | 68.928 | 40.661 |

Ranges are the two actual observations, not confidence intervals. These are
job timings excluding boot; natemu's full admission phases took 125.075/97.897
seconds including postprocessing/hash/fsck, whereas QEMU process wall times were
9.492–9.883/8.615–8.835 seconds. Do not compare those differently scoped wall
timers as identical measurements. Natemu's write/readback job is about 34–36 times
the QEMU job duration in these configurations. Passing the timeout gate has not
made I/O performance competitive. The subsequent repair below addresses the
measured coordination and invalidation overhead.

Both guests use the exact original kernel, initramfs, root image, fio binary,
profiles, 2 GiB RAM and 4 KiB/depth16/CRC workload; PCI support is built into the
stock kernel, so an attempted module-augmentation preparation was unnecessary
and never executed. QEMU uses `pc-i440fx-11.1`, `tcg,thread=multi`, CPU `max`,
one modern PCI virtqueue of 128, writeback caching and threaded native I/O;
indirect descriptors, EVENT_IDX, packed rings, queue reset, discard and write-zeroes
are disabled. Its maximum segments/segment size remain 126/4294967295 versus
natemu's 32/4096. Both guest queues report 1280 KiB max sectors and writeback.
QEMU's CPU profile and backend concurrency are not identical to natemu's.

Native fio is built from the unmodified upstream 3.39 tag using default configure
and ARM CRC support; only job filename and `io_uring`→`posixaio` change. It uses
APFS and `F_NOCACHE`, not Linux ext4/O_DIRECT. Native `aio_fsync` and QEMU's flush
path differ from natemu's `F_FULLFSYNC`; no durability equivalence is claimed.
Native achieved depth is mostly eight (depth16 about15.3% in write/readback and
7.2–7.5% in read-only), whereas both guests report depth16 about99.908%.
Runs were serialized under the shared lock, with no cache purge, in fixed order.
This tiny verification workload, especially subsecond native jobs, supplies
exploratory whole-system observations rather than sustained SSD scores, pure
translator overhead, or product-wide qualification. The campaign used all six
VM launches and four native jobs, about1.05GiB of its3GiB bound; no retries.

## Copy-first performance repair — November 8

The first repair (`candidate-build-06`, further corrected below) preserves copy-first exclusion,
complete range/chain validation, one active request/backend job, reset/S5 lifetime
rules and `F_FULLFSYNC`. It removes three avoidable costs: global mapping
invalidation for ordinary DMA writes, unconditional shadow eviction at every
window, and repeated capture of the same already-published descriptor prefix.
The [I/O plan](io-plan.md#implementation-kickoff--november-8) records the table-page
tracking and parked shadow-service contracts; its capture step specifies prefix
ownership, exhaustion, generation changes and fresh payload/suppression reads.

| Original 64 MiB / 4 KiB / depth16 CRC workload | Before repair, fio job seconds | Final primary / independent rebuild, fio job seconds |
| --- | ---: | ---: |
| Write + CRC readback | 68.928 | 3.122 / 3.050 |
| Separate read-only restart verification | 40.661 | 5.399 / 1.184 |

Write/readback is about 22 times faster in these observations. Restart verification
remains variable: report 1.184–5.399 seconds, not only its fastest result. Inline
readback takes 1.004/1.143 seconds, so the primary standalone-read slowdown does not
reproduce in the independent pair; its cause remains unresolved. QEMU's earlier
1.898–2.041/0.823–0.831 second jobs remain faster. The filesystem/CPU/device/durability
qualifications above still apply; these results do not establish sustained SSD
performance or close the general QEMU gap.

Both complete original pairs pass all profile/library checks, exact 64 MiB CRC
verification, persisted file hashes, clean S5, offline fsck and byte-identical
whole-image read-only restart. Full admission phases take 35.718/28.970 seconds
primary and36.256/33.487 seconds independent. Identical `/usr/bin/time -lp` wrappers
around the full two-phase runner report65.55/70.56 seconds elapsed and
120.64/130.42 CPU-seconds (user+system). These totals include boot, input hashes,
fio, guest postprocessing, restart, offline checks and child processes; they are
not pure I/O CPU measurements. No competing build/VM ran during timed pairs.

A 30-microsecond acknowledgement-spin candidate was rejected. It improved fio to
5.148/1.153 seconds but made the full pair 153.23 seconds/295.95 CPU-seconds, versus
100.20 seconds/190.16 CPU-seconds for the otherwise identical yielding version.
The final version keeps yielding and adds descriptor-prefix reuse. This is a
finite, fixed-order comparison, not an optimal polling-policy search.

The final source passes 175 host-library and 72 ABI tests (two existing tests remain ignored),
resident stack audit and 11 targeted live checks: N4 split-atomic windows/cache preservation,
high RAM, held compiler reply timeout/resume, reset/disable, full queue, malformed captured
successor, contained shutdown-sync error, and three network queue/IRQ cases. Earlier N1/N2/N4
and PTE/PDE checks pass; fresh independent final PTE/PDE replays also pass. An isolated late
shadow-reset control (a peer vCPU servicing a shadow-reset request late, after parking) forces
requests after all acknowledgements while release is withheld; the real resident services them,
while removing only that wait servicing produces the expected pre-grant deadline with clean
release/teardown. Malformed-later validates one valid completion followed by rejection with no
second backend effect; the alias unit test establishes owned-snapshot semantics, not a forced
live DMA alias.

All sources, build failures, diagnostics, intermediate candidates, rejected spin results and
final replays are retained at `natemu-raw/io-perf-repair-20261108-01`. `summary.json` and
`summarize.py` bind timings, source/binary/oracle manifests and all 39 of the 40 bounded VM
launches; storage is about 3.1 GiB of 4 GiB. Independent source/method and [final findings
review](../research/reviews/INV-03-01-findings.md#io-performance-repair-final-independent-rebuild-and-replay--november-8-2026)
are recorded in INV-03-01. No retries or weakened workloads were used to accept the final
candidate.

## I/O wake-up ordering — November 8

The selected follow-up is `io-perf-pipeline-20261108-01/candidate-build-01`.
Its only producing-source change from the first repair is in
`crates/runtime/src/execution/events.rs`: both HLT and firmware-mailbox loops now
**drain doorbells before checking the I/O request**. Previously, a request/kick
arriving between the checkpoint and drain could lose its only wake, leaving the
copy waiting for an unrelated timer or its acknowledgement deadline. The fix
preserves the same safe checkpoint before guest-memory access. Copy-first
exclusion, one native job, all completion/reset/S5 rules and F_FULLFSYNC remain.
No queue batching, polling-policy change, zero-copy or rust-vmm modification was
needed for this increment.

A retained diagnostic full pair identified acknowledgement waits as the dominant
window cost. Near the end of its write/CRC job, 32768 native jobs accumulated
0.149 seconds in native calls and 0.232 seconds in inclusive lane waits; these
are overlapping totals, not additive costs. The first 2048 boot windows alone
accumulated 22.57/26.51 seconds of acknowledgement waiting in the two processes.
Instrumentation and resident performance counters perturb execution: these are
mechanism diagnostics, not scored final timings.

The normal builds use the unchanged original 64 MiB, 4 KiB, depth16 fio profiles,
CRC readback, library/profile checks, persisted file hash, clean S5, offline fsck
and byte-identical whole-image read-only restart. All pairs below pass.

| Build/run | Write + CRC job | Restart verification job | Full pair elapsed | Full pair CPU, user + system |
| --- | ---: | ---: | ---: | ---: |
| Previous primary, candidate06 | 3.122 s | 5.399 s | 65.55 s | 120.64 s |
| Previous independent, candidate06 | 3.050 s | 1.184 s | 70.56 s | 130.42 s |
| Contemporaneous old replay, candidate06 | 4.072 s | 10.326 s | 104.24 s | 198.33 s |
| Fixed primary, candidate01 | 1.619 s | 0.989 s | 17.97 s | 25.02 s |
| Fixed fresh independent, candidate01 | 1.662 s | 0.992 s | 17.94 s | 24.84 s |

The two fixed full pairs agree closely. Old-build variation is substantial and
retained; the contemporaneous 5.81-times elapsed/7.98-times CPU improvement is
one fixed-order comparison, not a universal ratio. Full-pair totals include boot,
hashing, guest postprocessing, restart, offline checks and children. No competing
build or VM ran during timing. The fixed write/CRC jobs are below the earlier
QEMU 1.898–2.041-second observations, while restart reads remain above QEMU's
0.823–0.831 seconds. CPU, PCI/MMIO, backend/cache and durability differences above
still prevent an equivalent-device or general QEMU-parity claim. This remains a
finite correctness-verified workload, not sustained-storage characterization.

An independent adversarial reproduction holds each resident immediately before doorbell drain,
publishes a real copy request and confirms its GIC kick is pending. Both old loops hit the
specific acknowledgement deadline with no copy effects; both fixed loops complete the real copy.
All four pass exact RAM/state/teardown oracles. The mailbox control uses one vCPU directly in
the firmware wait path, not SMP startup; private wait counters do not sample the CPU's WFI
instruction. The [forced
reproduction](../research/reviews/INV-03-01-findings.md#io-wait-loop-doorbell-ordering--independent-forced-reproduction)
records patches, provenance, 36 negative parser controls and scope.

Nine final normal-build live regressions pass: HLT/AP window cases, N4 split atomics/cache
preservation, reset, disable, shutdown with a halted peer, malformed successor, full queue, and
network halt/IRQ. Fresh builds pass resident stack audits; the workspace normal CLI/resident are
rebuilt and signed from matching source. [Independent final
review/replay](../research/reviews/INV-03-01-findings.md#lost-io-wake-independent-source-review-control-review-and-replay--november-8-2026)
accepts the finite evidence and verifies all 916 producing inputs; only the wait-loop source
differs from candidate06. Evidence, hashes, timings, forced controls and final regressions are
retained at `natemu-raw/io-perf-pipeline-20261108-01`, with `summary.json`/`summarize.py`. The
increment used 21 of 24 bounded VM launches, including diagnostics and expected-negative
controls; no VM retry or weakened workload was used. An earlier forced-control preparation
anchor failure is retained separately and launched no VM.

### Post-fix profile: coordination versus copying

A fresh instrumented original write/restart pair after the wake-up repair passes
all original correctness checks. Both resident summaries are complete with no
failed/dropped log records. Evidence is at
`natemu-raw/io-perf-postwake-profile-20261108-01/summary.json`; the separate
diagnostic allowance used 2/2 VMs, each bounded at 240 seconds, and about 307 MiB
of 1 GiB. Production source and normal binaries are unchanged.

The last cumulative samples in each full VM phase show:

| Instrumented counter | Write/CRC phase | Restart-read phase |
| --- | ---: | ---: |
| Copy windows | 132096 | 67584 |
| Window total | 936 ms | 587 ms |
| Waiting for vCPU acknowledgements | 706 ms | 439 ms |
| Kicking vCPUs | 177 ms | 114 ms |
| Granted copy/invalidation section | 25 ms | 16 ms |
| Native backend execution | 149 ms | 28 ms |
| Inclusive native-lane wait | 249 ms | 88 ms |
| Block queue scans / empty results | 64512 / 30545 | 32768 / 15416 |

Counters are emitted every 1024 events, exclude the final partial intervals and
are not atomically sampled across counter families. Native execution includes
read-buffer allocation and flush; lane wait overlaps it and must not be added as
an independent cost. The granted section includes barriers and invalidation as
well as raw guest copies, but excludes other owned-buffer copies/allocations and
kernel copying. These instrumented timings are diagnostics, not replacement
scores for the normal builds above.

Approximately four windows occur per native job, and about 47% of block queue
scans are empty. Captured-prefix reuse is uncommon in this workload (183/159
cache-hit observations). This supports testing fewer metadata/empty-check
rendezvous first, for example a bounded metadata refresh appended to completion.
Such a change must preserve notification ordering so a stale empty capture cannot
consume a later kick; it is an assessment, not an implemented or accepted design.
Additional backend concurrency has less measured headroom here and should be
reconsidered for longer native I/O or broader workloads.

The data does not identify raw copying as the dominant cost. Zero-copy could still help by
eliminating global rendezvous, but that requires a separate safe ownership/coherency mechanism;
removing a memcpy alone does not supply it, and queue metadata/completions still need
synchronization. This motivated the fewer-round-trip experiment below while retaining owned
payloads. Remaining guest execution, MMIO and IRQ costs need further measurement. Large
sustained transfers and native networking are outside this diagnostic.

## Avoiding redundant block scans — November 8

Selected follow-up: `io-perf-notify-20261108-01/candidate-build-01`. The block
completion window reads the four-byte available header after all completion
writes, instead of only its two-byte suppression flags. The owner retires the
request and scans again only when that index indicates work, an already-owned
snapshot still contains a successor, or a notification arrived after admission.
It OR-preserves the notification flag under the existing state lock. No empty
snapshot is cached, no revision counter is introduced, and no next request is
admitted until the normal owner loop checks its lifecycle state. RNG/network
retain their previous rearm behavior. Copy-first buffers, native serialization,
all range checks, reset/disable/S5 and F_FULLFSYNC remain enforced.

Normal comparisons use the unchanged original full write/restart runner. The
primary order is old→candidate; a separately rebuilt candidate runs before the
old build in the independent comparison. All four pairs pass CRC, persistence,
S5, fsck and unchanged whole-image read-only restart checks.

| Observation | Write + CRC job | Restart verification job | Full pair elapsed | Full pair CPU |
| --- | ---: | ---: | ---: | ---: |
| Primary old | 1.653 s | 0.568 s | 17.27 s | 24.40 s |
| Primary candidate | 1.356 s | 0.935 s | 17.37 s | 23.85 s |
| Independent candidate | 1.393 s | 0.883 s | 17.15 s | 23.55 s |
| Independent old | 1.894 s | 0.925 s | 17.62 s | 25.08 s |

Write/readback takes 18–26% less time in these two comparisons. CPU direction is
modestly favorable in both. Complete workflow timing is effectively flat or a
small noisy gain, and restart reads are mixed: the first comparison is worse,
the reverse comparison slightly better. **No established full-workflow or
restart-read speedup is claimed.** Both the fastest old read and every other
sample are retained. These finite observations justify retaining a small safe
reduction of redundant work, not declaring I/O performance complete.

A final isolated diagnostic repeats the prior post-fix instrumentation. Last
periodic window counts fall from 132096 to 101376 in the write phase and 67584
to 52224 on restart (about 23% fewer). Empty block scans fall from 30545/15416 to
137/82. Native job counters have the same last periodic values, 33792/17408.
The diagnostic passes the full original oracle and both complete resident
summaries, but is not included as another scored timing pair. Every counter
family omits its final partial 1024-event interval and is sampled separately.

Validation: 177 host-library tests pass (two pre-existing ignores), including
16 retirement truth-table combinations, seven lifecycle gates and stale-snapshot
identity. Six reviewed single-vCPU forced controls comprise three healthy
schedules and three targeted counterfactuals: late notification preservation,
already-captured successor despite an aliased live index, and publication before
the completion capture. Every mutant must record exact idle/stranded work before
a separately traced rescue notification; all cases then finish three exact
native reads, full RAM/disk checks and clean teardown. The no-second-notify case
checks the implementation's fresh-index behavior, not a general permission for
drivers to omit required notifications. Notification progress assumes conforming
queue ownership; arbitrary aliases to device-owned used.flags gain no new promise.

Eight normal-driver regressions pass: reset, disable, halted-peer poweroff,
partial read/write failure, malformed successor, full queue and network halt/IRQ.
Independent source review and fresh rebuild/replay accept the scoped change;
all 916 producing inputs are bound and only three virtio source files differ
from the previous normal build. Workspace normal binaries are rebuilt/signed.
All sources, evidence and `summary.json`/`summarize.py` remain at
`natemu-raw/io-perf-notify-20261108-01`: 24/24 bounded VMs, about 2.5 GiB of 4 GiB.
The earlier test-hook trace-order correction and a rejected test-driver CLI mode
are retained; the CLI rejection happened before VM creation. No failed guest
schedule or correctness-invalid timing was accepted. Native networking, zero-copy,
broader sustained I/O and further performance work remain separate.

## Restart variance and redundant doorbells — November 8

This follow-up isolates restart reads by cloning the same previously verified
persisted image for each launch. The new optional `--restart-from` runner mode
requires a passed prior read-only verification, matching before/after/current
whole-image hashes and a valid persisted-file hash. It retains the original
fio/profile/library/CRC/S5/fsck checks. Default invocation still runs the original
write/restart pair. Input hashing warms the host file cache; these are not cold
physical-disk results. No guest affinity, cache purge, workload or durability
settings change.

The initial eight normal runs use predeclared order A B B A B A A B, where A is
the wake-order fix before empty-scan reduction and B is the selected empty-scan
reduction. Every run passes with identical disk and file contents:

| Build | Restart job seconds, in run order |
| --- | --- |
| A | 0.985, 0.724, 0.572, 0.973 |
| B | 0.909, 0.917, 0.512, 0.494 |

Both versions exhibit fast and slow runs, so the last block-scan change is not a
consistent restart regression. Fast B runs have substantially more fio context
switches (354/361 versus 38/41), but that alone does not identify the responsible
host or guest scheduling path.

Candidate C skips a native wake-up doorbell as redundant, only when the participant has already
acknowledged the exact newly published window epoch. Every callback still checks
whether its participant has ended under the existing stop mutex. The copier still
waits for every matching acknowledgement and retains cancellation, grant,
barriers and release ordering. The resident cannot resume guest access between
that acknowledgement and release. Old epoch acknowledgements never suppress a
new wake. No resident code, backend concurrency or memory authority changes.

A provisional candidate omitted the ended check on its skipped path. Independent
review caught the generic API contract gap before acceptance. Although Linux's
normal teardown cancels the pending permit first, those eight exploratory timings
are not final qualification: C 0.761/0.760/0.484/0.763 versus B
0.510/0.877/0.848/0.506, order C B B C C B B C. The corrected final candidate is
`candidate-build-02`; the reviewed source adds two focused tests and preserves
the terminal check. Final normal order C B B C gives:

| Build | Restart job | Restart process phase | Whole wrapper real / CPU |
| --- | ---: | ---: | ---: |
| C | 0.476 s | 7.627 s | 8.43 / 11.01 s |
| B | 0.909 s | 7.837 s | 8.65 / 11.54 s |
| B | 0.506 s | 7.794 s | 8.61 / 11.16 s |
| C | 0.749 s | 7.933 s | 8.75 / 11.51 s |

The candidate reduces redundant coordination work and both observed job-time
modes move in a favorable direction, but the sample is small, modes were not
controlled, and whole-phase/wrapper timing remains mixed. Do not present a
best-case ratio as a general speedup or claim the variability is fixed.

Validation so far: 179 host tests pass with two existing ignores. The new
four-participant/two-epoch test goes red before the optimization, then verifies
that exact acknowledgements suppress only the unnecessary rings; the second
test rejects an acknowledged but ended participant without guest effects.
Four live checks pass: N=4 split atomics/code preservation, a delayed compiler
reply, reset cancellation, and halted-vCPU wakeups. Independent source review
accepts the corrected code, and a fresh independent build repeats the original
full write/restart pair. Both full pairs preserve CRC, exact I/O counts,
persistence, clean S5/fsck and byte-identical read-only restart images:

| Original full workflow | Write + CRC job | Restart job | Whole real / CPU |
| --- | ---: | ---: | ---: |
| Primary final candidate | 1.331 s | 0.768 s | 17.03 / 23.60 s |
| Fresh independent candidate | 1.375 s | 0.488 s | 17.40 / 23.85 s |

Four isolated **baseline B** profiles identify an association with guest work
placement. The profiler classifies the pinned file's physical extents and checks
all 16,384 unique 4-KiB reads. Its envelope starts at first native admission and
ends at last successful guest completion; it excludes first-request metadata
capture and final guest CRC/reporting, so it is not fio's internal job timer.
All four classifiers and original workload oracles pass. Every vCPU has natural
HVC snapshots at both boundaries, with no extra exits/kicks or guest-RAM sampling.

| Profile | Fio job / data envelope | Window total / ACK wait | Native execution | Measured copy portion | Notify-access CPU |
| --- | ---: | ---: | ---: | ---: | --- |
| 1 | 0.499 / 0.483 s | 215 / 146 ms | 18.7 ms | 6.8 ms | CPU 0 |
| 2 | 0.500 / 0.484 s | 213 / 142 ms | 18.6 ms | 6.9 ms | CPU 0 |
| 3 | 0.916 / 0.900 s | 420 / 316 ms | 18.3 ms | 7.4 ms | CPU 1 |
| 4 | 0.789 / 0.774 s | 385 / 287 ms | 19.0 ms | 7.3 ms | Mostly CPU 1 |

IRQ-acknowledgement accesses remain on CPU 1 in every profile. Fast runs place
16,383 observed notify accesses on CPU 0; the slowest places all 16,383 on CPU 1.
Its CPU 0 resident halted bucket accounts for about 877 ms, versus 8 ms in the
first fast run. That bucket includes I/O checkpoint work while halted: CPU 0
still uses 414 ms of host thread CPU in the slow sample, so it is not evidence
that macOS simply slept the thread. Both active guest CPUs make progress in the
fast samples. This supports guest work placement/coordination as the next target;
it does not prove the fio thread's identity/placement or a causal scheduling fix.
Native disk execution and the measured copy portion do not explain the roughly
400-ms variation. Zero-copy is not required to investigate this remaining cost.

Counter families are independently sampled and their scopes overlap (native
execution is included in lane wait; window ACK wait is included in window time).
The copy portion also includes barriers/invalidation, and excludes other owned
buffer/kernel copies. Natural-HVC snapshot skew is explicit: up to 4.33 ms for
the mostly halted CPU, at most 83 microseconds for the other observed boundaries.
All final log failed/dropped counters are zero. The diagnostic uses the baseline
with unconditional rings; it does not directly measure the candidate's skipped
ring count or establish a causal explanation of the speedup.

The next bounded performance investigation should isolate guest placement and
halted-peer coordination without turning affinity or workload tuning into the
product fix. Whole-workflow improvement and general QEMU parity remain open.
Normal workspace CLI/resident are rebuilt and signed; diagnostic code exists
only in the retained isolated snapshot.

Evidence is retained under `natemu-raw/io-perf-restart-20261108-01`, bounded to
32/32 VMs (all pass), 240 seconds each and 4 GiB. Successful byte-identical read-only images
are deduplicated to one inode with every path/content/hash retained; archived
images must not be modified, and replay clones its input. Failed artifacts are
never removed or replaced by successful retries.

## Ordinary disk use and scaling matrix — November 8

Measured with the selected, uninstrumented exact-ACK doorbell build, pinned fio 3.39 and QEMU
11.1.2 TCG on the M4 (10 host CPUs, 16 GiB RAM), macOS 26.6.2 (25G83). Each guest has 2 GiB RAM
and the same 2 GiB raw ext4 root, stock Debian 6.12.107 kernel/modules, BusyBox and fio. Four
256 MiB files provide 1 GiB of data; single-job profiles use one 256 MiB file. Native host
scores from the earlier 64 MiB workload are not comparable new-matrix baselines.

**Result: all 16 VM launches (eight main/relaunch pairs) pass, including independent N4
repeats.** The harness also passes 396 negative controls, six valid serial cases and guest shell
syntax checks. No production code changed.

**Acceptance:** each pair boots/mounts the installed root, performs
create/overwrite/copy/compare/rename/unlink on known 4 MiB fixture data, checks that
renamed/unlinked source paths are absent, preconditions all four files with CRC32C, runs all
nine workload profiles, verifies all 1 GiB, records file hashes, remounts read-only and powers
off. Offline fsck follows; a fresh read-only VM verifies all 1 GiB again and matches every saved
hash while the entire disk image remains byte-identical. This uses a scripted installed BusyBox
root, not a full distribution init/login acceptance test.

Eight timed profiles run for 5 s each. Pure-read profiles verify CRC inline; writes generate 4
KiB CRC headers (including 128 KiB requests). Mixed reads are not verified inline; the later
full sweep checks stored data after mixed writes. serialize_overlap prevents workload-induced
overlapping writes. The finite fsync case writes 64 × 4 KiB, reports 63 periodic sync samples
and performs a required final end_fsync, which fio excludes from its sync count/latency
distribution. Final-sync errors are rejected. These are checksum-bearing guest workloads, not
raw device bandwidth.

Preconditioning and hash checks warm host caches; guest direct I/O does not bypass the macOS
backing-file cache. No affinity, host-cache purge, queue tuning or production code changes. QEMU
uses the prior disclosed PCI/features/threaded backend, versus natemu MMIO/one native request;
QEMU fsync and natemu F_FULLFSYNC are different durability paths. Large-request advantages can
include translated checksum CPU work and must not be called SSD speedups.

Primary order: natemu N1, QEMU N1, QEMU N2, natemu N2, natemu N4, QEMU N4. Independent repeated
N4 order: QEMU N4, natemu N4, using the independently built matching natemu binaries. Every row
below retains the primary result; repeats are separate, not substituted for slower samples.

**Primary throughput and latency:** each cell is natemu / QEMU. Higher throughput is better;
lower time/latency is better.

| Workload | 1 vCPU | 2 vCPUs | 4 vCPUs |
| --- | ---: | ---: | ---: |
| Sequential read, 4 KiB Q16, MiB/s | 116.434 / 63.594 | 85.809 / 75.810 | 35.035 / 75.278 |
| Sequential read, 128 KiB Q16, MiB/s | 1765.025 / 153.862 | 2220.225 / 153.324 | 983.125 / 156.905 |
| Sequential write, 128 KiB Q16, MiB/s | 808.321 / 137.994 | 757.206 / 146.104 | 709.816 / 142.183 |
| Random read, 4 KiB Q1, kIOPS | 20.222 / 12.051 | 12.319 / 12.021 | 6.185 / 11.114 |
| Random read, 4 KiB Q16, kIOPS | 27.201 / 16.104 | 22.775 / 14.511 | 8.784 / 19.103 |
| Random read, 4 KiB Q64, kIOPS | 27.677 / 16.550 | 22.201 / 19.756 | 12.604 / 15.724 |
| Four random-read jobs, 4 KiB Q16 each, kIOPS | 26.938 / 16.014 | 33.394 / 28.358 | 18.272 / 55.652 |
| Mixed random 70/30, 4 KiB Q16, total MiB/s | 99.984 / 71.764 | 125.730 / 116.890 | 42.460 / 118.201 |
| 64 writes with fsync, total job seconds | 0.603 / 0.020 | 0.638 / 0.023 | 0.626 / 0.016 |

| Random-read p99 completion latency, ms | 1 vCPU | 2 vCPUs | 4 vCPUs |
| --- | ---: | ---: | ---: |
| Q1 | 0.076 / 0.075 | 0.093 / 0.069 | 0.334 / 0.057 |
| Q16 | 0.913 / 1.155 | 0.782 / 1.090 | 2.408 / 0.864 |
| Q64 | 2.900 / 4.424 | 2.998 / 3.293 | 6.914 / 4.178 |
| Four jobs, Q16 each; worst per-job p99 | 16.908 / 17.170 | 24.248 / 6.390 | 12.911 / 4.293 |

Aggregate concurrent-job rates divide all bytes/I/Os by the union of
job_start..job_start+job_runtime intervals (all jobs overlap). Latency percentiles are retained
per job; the four-job table shows the worst per-job p99, not an aggregate percentile. Full
per-direction rates, p50/p95/p99, actual read fractions and achieved-depth distributions remain
in summary.json and raw fio JSON.

**Independent N4 repetition:**

| Workload | Natemu primary / repeat | QEMU primary / repeat |
| --- | ---: | ---: |
| Sequential read, 4 KiB Q16, MiB/s | 35.035 / 45.537 | 75.278 / 74.016 |
| Sequential read, 128 KiB Q16, MiB/s | 983.125 / 971.550 | 156.905 / 159.250 |
| Sequential write, 128 KiB Q16, MiB/s | 709.816 / 733.104 | 142.183 / 143.141 |
| Random read, 4 KiB Q1, kIOPS | 6.185 / 6.204 | 11.114 / 11.447 |
| Random read, 4 KiB Q16, kIOPS | 8.784 / 9.237 | 19.103 / 17.144 |
| Random read, 4 KiB Q64, kIOPS | 12.604 / 9.077 | 15.724 / 19.596 |
| Four random-read jobs, 4 KiB Q16 each, kIOPS | 18.272 / 18.985 | 55.652 / 54.702 |
| Mixed random 70/30, 4 KiB Q16, total MiB/s | 42.460 / 34.896 | 118.201 / 119.013 |
| 64 writes with fsync, total job seconds | 0.626 / 0.631 | 0.016 / 0.022 |

**Combined VM process totals:** main workload boot plus read-only verification boot, including
guest CRC/hash work. These exclude outer cloning/hashing/offline fsck; the separately retained
wrapper_seconds includes those. They are not directly the earlier full-admission-wrapper timer.

| Primary configuration | Natemu elapsed / host CPU, seconds | QEMU elapsed / host CPU, seconds |
| --- | ---: | ---: |
| 1 vCPU | 126.06 / 156.12 | 211.78 / 225.61 |
| 2 vCPU | 122.35 / 222.00 | 197.43 / 238.64 |
| 4 vCPU | 191.25 / 593.97 | 197.29 / 256.18 |

| Mounted-root observation, seconds (primary main / relaunch) | Natemu | QEMU |
| --- | ---: | ---: |
| 1 vCPU | 2.026 / 2.039 | 2.829 / 2.828 |
| 2 vCPU | 2.275 / 2.049 | 2.563 / 2.555 |
| 4 vCPU | 2.273 / 2.542 | 2.839 / 2.841 |

| Periodic fsync latency, ms (primary mean / p99; final sync excluded) | Natemu | QEMU |
| --- | ---: | ---: |
| 1 vCPU | 8.270 / 10.813 | 0.105 / 1.892 |
| 2 vCPU | 9.145 / 11.207 | 0.187 / 1.548 |
| 4 vCPU | 9.103 / 14.352 | 0.130 / 1.565 |

**Observed result:** natemu preserves ordinary filesystem and persistence behavior in every
tested pair, but four-vCPU small-I/O scaling is a clear weakness in these runs. Its random-read
Q16 rate is 8.78/9.24 kIOPS in primary/repeat, and four-job throughput is 18.27/18.98 kIOPS
against QEMU 55.65/54.70 kIOPS. The primary N2 four-job result favors natemu throughput (33.39
versus 28.36 kIOPS) while worsening the worst per-job p99 (24.25 versus 6.39 ms). Large 128 KiB
transfers favor natemu in both N4 runs. The primary N4 combined elapsed times are close (191.25
versus 197.29 s), but natemu consumes 593.97 versus 256.18 host CPU-seconds. More vCPUs do not
automatically improve this device path. These finite samples do not establish statistical
confidence or isolate the cause; coordination and fairness merit the next bounded investigation
before making a zero-copy decision.

Root-mounted markers are host observations polled every 250 ms, with guest uptime also retained;
they are not full-distribution readiness times. Filesystem and persistence checks pass
independently of throughput rankings.

Evidence: natemu-raw/io-matrix-20261108-01, including frozen oracle-01/inputs-02, source/binary
bindings, all original fio JSON, host timing, kernel/serial logs, offline fsck, per-file hashes,
and independent replay. Preparation inputs-01 and the initial native sync-count assertion
failure are retained; the final 63-sync oracle is supported by fio source and native
observations. No failed VM is replaced by a passing retry.

Independent [evidence
review](../research/reviews/INV-03-01-findings.md#installed-root-io-matrix--independent-review-and-n4-replication-november-8-2026)
accepts this bounded result: all eight pairs were reparsed, all 72 workload rows recomputed, the
published throughput/latency cells checked, and both replica final image hashes independently
verified. The review retains the scaling, CPU, durability and workload limitations above.

Coverage limits: one disk, whole-device ext4, direct-kernel boot and scripted userspace.
Partitioned disks, a full distro/login, guest-initiated reboot, disk-full/crash recovery, native
networking and long-duration/cold-host-storage behavior are not qualified by this matrix.

Resource use: 16 of 20 allowed VM launches, no VM failures/retries, approximately 10.72 GiB
retained against the 12 GiB bound. Four reserve launches remain unused; the campaign is
complete.

## HLT coordination repair — November 8

The selected change lets a resident publish an ordered no-guest-memory promise
while sleeping in HLT. The I/O coordinator can admit that participant without
waking it. Withdrawal is ordered against request publication, and the resident
must checkpoint before touching guest RAM. Terminal-state and cancellation
checks remain. AP-mailbox waits still acknowledge each request. A sleeping CPU
can skip several epochs, so table invalidation compares the latest table write
with its previously serviced epoch, rather than only the latest request. Native
backends still operate on owned buffers; this is a copy-first coordination fix.

The original N4 two-profile probe reproduced 9.35k / 18.09k random-read IOPS.
A read-first shadow-bit check gave 8.85k / 18.09k and was withdrawn. A 10-us ACK
spin gave 13.40k / 23.32k but raised whole-probe CPU from 63.21 to 65.69 seconds
and worst four-job p99 from 19.01 to 23.20 ms; it was also withdrawn. Neither is
part of the selected patch. Isolated baseline profiling and independent replay
measured 6.720 / 6.390 seconds waiting for ACKs versus 0.224 / 0.233 seconds in
the grant/invalidation/copy/release phase. The latter is not pure memcpy time.
HLT-context waits account for 35.4% / 44.5% of summed resident I/O wait, including
initial HLT checkpoints; these percentages are not recoverable wall-time estimates.

The full comparison retains exactly the earlier matrix's fio jobs, stock kernel,
2 GiB RAM/root image, four 256 MiB files, CRC and ordinary-filesystem checks,
flush semantics, clean shutdown, fsck and fresh read-only verification. Order:
fresh baseline N4, candidate N4/N2/N1, independent fresh-build candidate N4,
then fresh baseline N1 and candidate N1 under the declared follow-up allowance.
There are no concurrent builds/benchmarks, cache purges, affinity changes or
instrumentation in scored builds. Eight timed jobs are five seconds each; the
fsync job performs 64 writes. Rates include checksum work and warm host caches.
QEMU values below are the already measured pinned 11.1.2 comparison, not new
runs in this repair campaign. Its fsync durability path still differs from
natemu's F_FULLFSYNC.

**Four vCPUs, full matched workflow:**

| Workload / measure | Fresh baseline | Candidate | Independent candidate | Earlier QEMU reference |
| --- | ---: | ---: | ---: | ---: |
| Sequential read, 4 KiB Q16, MiB/s | 44.616 | 144.798 | 124.559 | 75.278 |
| Sequential read, 128 KiB Q16, MiB/s | 1249.100 | 2669.125 | 2109.025 | 156.905 |
| Sequential write, 128 KiB Q16, MiB/s | 689.531 | 792.110 | 453.543 | 142.183 |
| Random read, 4 KiB Q1, kIOPS | 6.218 | 14.637 | 14.877 | 11.114 |
| Random read, 4 KiB Q16, kIOPS | 9.228 | 36.618 | 36.983 | 19.103 |
| Random read, 4 KiB Q64, kIOPS | 11.041 | 30.042 | 37.769 | 15.724 |
| Four readers, 4 KiB Q16 each, kIOPS | 18.437 | 46.318 | 44.958 | 55.652 |
| Mixed 70/30, 4 KiB Q16, total MiB/s | 43.719 | 132.685 | 113.998 | 118.201 |
| 64 fsync writes, job seconds | 0.587 | 0.589 | 0.694 | 0.016 |
| Q16 read p99, ms | 2.474 | 0.569 | 0.518 | 0.864 |
| Four readers, worst per-job p99, ms | 15.270 | 12.780 | 8.159 | 4.293 |
| Periodic fsync mean, ms | 8.425 | 8.506 | 10.175 | 0.130 |
| Periodic fsync p99, ms | 10.682 | 16.908 | 31.326 | 1.565 |
| Two VM processes, elapsed seconds | 193.760 | 113.070 | 114.730 | 197.290 |
| Two VM processes, host CPU-seconds | 599.430 | 215.550 | 205.150 | 256.180 |

**One vCPU, including the slower first sample and the bounded fresh comparison:**

| Workload / measure | Fresh baseline | Candidate first | Candidate repeat |
| --- | ---: | ---: | ---: |
| Sequential read, 4 KiB Q16, MiB/s | 117.676 | 108.968 | 114.536 |
| Sequential read, 128 KiB Q16, MiB/s | 1761.550 | 1719.500 | 1770.400 |
| Sequential write, 128 KiB Q16, MiB/s | 436.446 | 501.162 | 711.971 |
| Random read, 4 KiB Q1, kIOPS | 20.027 | 20.425 | 20.330 |
| Random read, 4 KiB Q16, kIOPS | 27.223 | 27.032 | 28.384 |
| Random read, 4 KiB Q64, kIOPS | 26.669 | 26.796 | 28.016 |
| Four readers, 4 KiB Q16 each, kIOPS | 26.483 | 25.711 | 27.046 |
| Mixed 70/30, 4 KiB Q16, total MiB/s | 91.436 | 90.940 | 98.461 |
| 64 fsync writes, job seconds | 0.642 | 0.582 | 0.627 |
| Q16 read p99, ms | 0.897 | 0.905 | 0.897 |
| Four readers, worst per-job p99, ms | 16.908 | 16.908 | 16.908 |
| Periodic fsync mean, ms | 9.002 | 8.130 | 8.575 |
| Periodic fsync p99, ms | 11.731 | 18.219 | 11.207 |
| Two VM processes, elapsed seconds | 126.520 | 127.500 | 126.600 |
| Two VM processes, host CPU-seconds | 154.660 | 153.710 | 156.020 |

**Two vCPUs:** the baseline is the earlier matrix, not a fresh matched run.

| Workload / measure | Earlier baseline | Candidate |
| --- | ---: | ---: |
| Sequential read, 4 KiB Q16, MiB/s | 85.809 | 112.619 |
| Sequential read, 128 KiB Q16, MiB/s | 2220.225 | 2354.575 |
| Sequential write, 128 KiB Q16, MiB/s | 757.206 | 786.936 |
| Random read, 4 KiB Q1, kIOPS | 12.319 | 14.745 |
| Random read, 4 KiB Q16, kIOPS | 22.775 | 35.640 |
| Random read, 4 KiB Q64, kIOPS | 22.201 | 28.393 |
| Four readers, 4 KiB Q16 each, kIOPS | 33.394 | 33.614 |
| Mixed 70/30, 4 KiB Q16, total MiB/s | 125.730 | 120.385 |
| 64 fsync writes, job seconds | 0.638 | 0.610 |
| Q16 read p99, ms | 0.782 | 0.561 |
| Four readers, worst per-job p99, ms | 24.248 | 26.870 |
| Periodic fsync mean, ms | 9.145 | 8.840 |
| Periodic fsync p99, ms | 11.207 | 11.338 |
| Two VM processes, elapsed seconds | 122.350 | 115.380 |
| Two VM processes, host CPU-seconds | 222.000 | 194.610 |

Rates use the union of each concurrent job's start/end interval. The four-reader
p99 is the worst per-job percentile, not an averaged or reconstructed aggregate.
Full p50/p95/p99 and achieved-depth distributions remain in raw fio JSON and
summary.json. Combined VM time includes boot, CRC/hash work and the read-only
relaunch, excluding outer image cloning/hashing/fsck. Periodic fsync statistics
exclude fio's required final end_fsync; final-sync errors remain fatal.

**Interpretation:** the large N4 small-I/O and CPU regression is repaired in both
primary and independent execution. Four-reader throughput and tail latency still
trail QEMU. The N2 four-reader rate is approximately unchanged, with a higher
worst p99 in this sample. N1 and large-write limitations are assessed below;
this is not a claim that every workload improves or that the native storage
backend is uniformly faster. Short samples do not establish statistical bounds.

The first N1 candidate write result was 501.16 MiB/s versus the earlier baseline's
808.32 MiB/s, prompting a separately recorded four-VM extension. In the fresh
back-to-back N1 comparison, the unchanged baseline writes at 436.45 MiB/s and
the candidate at 711.97 MiB/s; combined VM elapsed is 126.52 / 126.60 seconds
and CPU is 154.66 / 156.02 seconds. A consistent candidate-specific write
regression does not reproduce. This does not explain the variability or prove
all N1 effects negligible: the first candidate and independent N4 write samples
remain visibly slower. N4 writes range from 453.54 to 792.11 MiB/s across the
two candidate runs. No uniform write-speed gain is claimed.

Host context during the matched N1 runs records 96% APFS capacity, about
9.48 / 8.69 GiB available, 1,077.69 MiB of host swap used, and no recorded
thermal/performance warning from pmset. These point-in-time observations do not
identify the cause, prove absence of memory pressure or establish conditions
throughout earlier runs. Process rusage swap counts are distinct from host swap
usage. No host-cache purge, settings change or data deletion intervened between
these matched runs.

**Correctness and review:** 182 host and 72 ABI unit tests pass (two host tests
remain ignored). Three new host tests cover repeated quiet admission without
synthetic ACKs, withdrawal before grant, and terminal quiet participants. The
forced two-CPU test warms a global mapping and cached function, leaves both
CPUs in HLT across three grants (table/code write in epoch 2 followed by a
read-only epoch 3), then wakes them with interrupts. Both must observe the new
mapping/function, record the interrupt and end with ACK[3,3]/quiet[0,0]. Primary
and independently rebuilt runs pass. A resident mutant restoring the old
latest-request table check is rejected exactly for both stale data results;
code/IRQ/epoch/teardown checks still pass. Independent review strengthened final
ACK checking, added its failing self-control, and rejudged the retained positive
and negative reports with the corrected oracle. The cancellation fixture tests
a pre-canceled request that announces no epoch, not cancellation after announce.

The normal N4 split-atomic/code fixture, AP-mailbox wake fixture and a forced
late shadow-reset request after four exact ACKs all pass. The late shadow-reset test uses
only a retained host injection and the unchanged candidate resident; its older
missing-service negative control remains in the prior repair campaign. This
covers active parked service; it does not deterministically force every race
between HLT withdrawal and shadow requests. Source review supplies the ordering
argument; these finite tests are not a formal memory-model proof.

All seven full matrix pairs pass boot/mount, ordinary file operations, CRC and
whole-file hashes, clean S5, offline fsck and fresh read-only persistence with an
unchanged final image. The campaign uses 28 VMs: 27 healthy runs and one deliberate
stale-table failure, with no unexpected VM failure or retry. The original 24-VM /
8-GiB allowance was explicitly extended to 28 / 11 GiB for the material N1 write
concern. Reproducible Cargo intermediates were removed after builds/primary runs;
all bound binary hashes, source snapshots, recipes, logs and disk images remain.
Approximately 10.14 GiB is retained within the revised 11-GiB bound.

The [final independent
review](../research/reviews/INV-03-01-findings.md#hlt-quiescence--final-independent-acceptance-november-8-2026)
accepts this targeted N4 improvement, independently recomputing all 63 workload rows and
checking all 135 published numeric cells. It retains the write/fairness limitations and finds no
substantive blocker in the selected increment.

Evidence: `natemu-raw/io-smp-fix-20261108-01`, including the failed candidate
screens, isolated profiler and replay, candidate-build-03, replica-build-01,
stronger quiet oracle/negative control, regressions-03, all matrix directories,
summary.json and source/binary/output bindings. The first full baseline wrapper
retains its original outer timeout; it completed normally. Review removed that
outer timeout for subsequent runs because killing only the runner could orphan
a separately supervised VM; the reviewed inner 600/120-second bounds remain.

Coverage remains one disk, whole-device ext4, direct-kernel boot and scripted
userspace. Full distribution startup, partitions, guest reboot, disk-full/crash
recovery, native networking and long/cold-storage behavior are not qualified.
The patch introduces neither zero-copy nor weaker durability.


## Safety stress performance — November 8

The safety extension keeps normal runtime instructions unchanged: independent ELF comparison
finds the entire `.boot` and `.text` identical to the earlier accepted HLT-quiescence candidate
build, which later sections call the control build. Test hooks are feature-gated and built
separately. This is additional correctness/load evidence, not a new optimization or a zero-copy
experiment. See [the safety coverage](io-plan.md#io-safety-stress--november-8).

`io-safety-stress-20261108-01/soak-primary-02` passes its main and fresh read-only
VMs. The stock Linux guest has four vCPUs, 2 GiB RAM, one 512-MiB ext4 disk and
two disjoint 64-MiB files. Both fio 3.39 jobs use 4-KiB direct io_uring at Q16:
one immutable CRC-verified random-read file and one CRC-tagged random-write file.
After a 60-second unloaded context and full verification, the same jobs run for
300 seconds with two separately seeded stress-ng `int64` CPU workers and one
256-MiB `one-zero` memory worker. Each stress worker runs for 360 seconds with
verification; actual worker PID/starttime/progress observations span the load.
The timed memory worker may interrupt its final pass; completed passes are checked.

| Primary run | Unloaded read | Loaded read | Unloaded write | Loaded write |
| --- | ---: | ---: | ---: | ---: |
| IOPS | 26,376.65 | 11,285.58 | 16,427.77 | 7,828.65 |
| MiB/s | 103.034 | 44.084 | 64.171 | 30.581 |
| Completion p50, ms | 0.522 | 0.856 | 0.840 | 0.987 |
| Completion p95, ms | 0.741 | 3.785 | 1.434 | 6.128 |
| Completion p99, ms | 0.946 | 6.324 | 2.056 | 8.290 |
| Completed I/Os | 1,583,390 | 3,385,617 | 986,241 | 2,348,854 |

An independent fresh normal CLI/resident build repeats the full pair:

| Independent run | Unloaded read | Loaded read | Unloaded write | Loaded write |
| --- | ---: | ---: | ---: | ---: |
| IOPS | 27,123.54 | 11,618.05 | 16,924.77 | 8,019.59 |
| MiB/s | 105.951 | 45.383 | 66.112 | 31.327 |
| Completion p50, ms | 0.514 | 0.848 | 0.823 | 1.004 |
| Completion p95, ms | 0.725 | 3.817 | 1.401 | 5.865 |
| Completion p99, ms | 0.872 | 6.128 | 1.892 | 7.766 |
| Completed I/Os | 1,628,036 | 3,485,381 | 1,015,808 | 2,406,213 |

These fixed-order observations show contention under this combined workload;
they are not randomized causal comparisons or matched QEMU/native benchmarks.
They also differ from the earlier matrix's file sizes and concurrent job mix.
The host cache is not evicted and images are APFS clones; `direct=1` bypasses
the guest page cache, not every host cache. Writes use `end_fsync=1`, and the
backend retains its full-sync/flush contract. These are not per-write durable
IOPS or sustained cold-SSD measurements.
The longest retained loaded bandwidth/IOPS/histogram sampling gap is 1.496
seconds. Histograms retain their actual timestamps and sample counts, including
incomplete trailing intervals; they are not represented as containing every
completion. CPU workers report 336,390 and 327,715 bogo operations, the memory
worker 49,758,208, each reporting one passed worker and zero failed/skipped workers. These counts
establish progress in the selected verified methods, not cross-platform CPU scores.

The primary main/restart processes take 482.66/11.00 seconds and
1,773.35/13.91 host CPU-seconds. Whole-process time includes boot, integrity
sweeps and paced evidence export; fio's job metrics exclude later export.
Both images pass offline fsck; full file hashes persist and the read-only
restart leaves every image byte unchanged. Raw job JSON, 1-second samples,
worker logs, provenance, guest markers, hashes and fsck results remain bound
to the frozen binaries and inputs.
The independent main/restart take 483.16/11.25 seconds and 1,787.97/13.10 host
CPU-seconds, with the same integrity/persistence results. Its longest loaded
sample gap is 1.270 seconds. Both jobs overlap for 299.990 seconds primary and
300.000 seconds independent, inside the verified workers' measured lifetimes.
The accepted pairs record 16,839,540 timed I/O completions in total. Reads verify
inline; tagged writes receive full-file verification afterward, not individual
readback of every overwritten write.

The original `soak-primary-01` is retained as **failed**, despite completed fio,
worker and CRC checks: at guest uptime 456.6 seconds Linux reported an RCU stall
during raw multi-megabyte UART log export. Its intact read/write rates were
26,347.9/16,090.9 IOPS unloaded and 11,976.1/8,111.4 loaded; p99 was
0.930/2.089 and 5.997/7.700 ms. These remain descriptive failed-run observations.
The replacement exports every log losslessly with gzip/base64, exact byte length
and SHA256, bounded host decoding and at most eight 76-character lines before a
50-ms pause. Kernel warnings are neither suppressed nor ignored. The underlying
bulk-serial RCU stall remains unexplained; transport pacing does not fix or
qualify sustained UART behavior. Its altered export cost also prevents comparing
whole-VM times with the original raw-export run as an I/O speed improvement.

Normal native fixtures completed 512 cycles per run; internal stress elapsed
time at N2 was 0.580/0.621 seconds
for the two seeds; N4 took 1.304/0.959 seconds. These are bounded scheduling-test
durations, not application IOPS. All normal cycles verify exact shared increment
counts as well as NOT parity, and changed code/PTE visibility. Forced schedules
and reset/disable tests retain their elapsed/CPU observations separately; their
test synchronization and small duration make throughput comparisons misleading.

Independent N4 stress repeats 512 cycles in 1.330 seconds; forced N2 repeats
64 cycles, and reset/disable each repeat 64 generations. The independent driver
also rejects the pinned missing-service mutant for the intended reason. Across
the campaign, 2,560 normal cycles, 320 forced cycles and 256 lifecycle generations
pass. Host/ABI unit tests pass 254 with two existing ignored tests. The default
workspace build is restored without test hooks; no production optimization is
introduced by this coverage increment.

All 24 launches are accounted for: 18 healthy VMs, three deliberate negative controls and three
unexpected failed attempts (the two fixture-MMIO errors and the original serial-export stall).
The independent audit reparses cycle counters, lifecycle native-call/commit traces, decoded
logs, hashes and both full soak pairs. All failures, sources, binaries, commands and results are
retained under `natemu-raw/io-safety-stress-20261108-01`, about 2.32 GiB within the 4-GiB bound.
The [independent
review](../research/reviews/INV-03-01-findings.md#independent-fresh-build-replay-and-final-disposition)
accepts the stated finite I/O safety coverage, with the UART limitation above still open; it is
not acceptance of all ordinary-VM behavior.

### Sustained UART reproduction and readiness tuning — November 8

A serial-only stock Debian 6.12.107 guest, with four vCPUs and one pinned
heartbeat process per CPU, reproduces the earlier export stall without disk
I/O. The original unpaced payload is 4,656,640 guest bytes, or exactly 4,714,848
wire bytes after the configured CRLF conversion. The old instantaneous UART
produces an RCU warning inside that byte stream and a 26.56-second heartbeat
gap on its serial-IRQ CPU. Pacing identical bytes in 64-KiB chunks with 50-ms
pauses passes, with a maximum gap of 1.59 seconds. This is a batching/pacing
counterfactual, not an isolated estimate of native sink cost.

The repair separates byte acceptance from empty/readiness publication: bounded
16-byte host staging spills full batches synchronously, while a timer publishes
THRE/TEMT and the transmit interrupt after the last accepted byte's inactivity
interval. It retains all REP OUTS bytes and does not emulate FIFO overflow or
physical baud timing. The first 50-us candidate transfers the complete payload
without a stall but **fails** its final marker check: immediate `poweroff -f`
leaves output queued inside Linux. That attempt remains failed. New immutable
inputs add an explicit final BusyBox `stty` operation using `TCSADRAIN`; the
payload and heartbeat remain unchanged. Exact BusyBox 1.37.0 source and Debian
patch inspection establish that explicit settings invoke this drain even when
unchanged. Repeating the old UART with this final drain still produces the RCU
stall and a 25.52-second heartbeat gap.

| UART / workload | Result | Guest output, s | Whole VM elapsed, s | Host CPU, s | Largest CPU heartbeat gap, s |
| --- | --- | ---: | ---: | ---: | ---: |
| Old, original unpaced | RCU/byte-stream failure | 26.56 | 34.27 | 37.87 | 26.56 |
| Old, identical bytes paced | Pass | 30.07 | 38.11 | 36.84 | 1.59 |
| Old, unpaced with final drain | RCU/byte-stream failure | 25.53 | 33.29 | 36.75 | 25.52 |
| 50 us, before final-drain correction | Final marker absent | 38.89 | 47.72 | 37.03 | 1.04 |
| 50 us, original with final drain | Pass | 38.85 | 47.45 | 36.73 | 1.04 |
| 50 us, triple payload with final drain | Pass | 117.18 | 126.20 | 99.88 | 1.03 |
| 20 us, original with final drain | Pass | 29.88 | 38.20 | 36.51 | 1.04 |
| 20 us, triple payload with final drain | Pass | 90.31 | 98.86 | 99.77 | 1.04 |

Host CPU is user plus system time for the entire VM process, including boot and
export; it is not payload-only CPU. Failed timings are observations, not accepted
performance results. The triple payload is exactly 14,144,544 wire bytes. Every
passing candidate has exact bytes/hash, advancing IRQ and TIMER/RCU counters,
all four CPU heartbeats, no kernel error, the final marker and normal S5.
The shared final oracle explicitly rejects any heartbeat gap above two seconds;
its boundary self-check accepts 2.00 and rejects 2.01 seconds. Rejudging both
50-us successes with that oracle passes. Sustained output lasts well beyond the
original stall threshold, so these successes are not simply shorter transfers.

The isolated 20-us snapshot changes only the readiness constant from the 50-us
snapshot; both retain the same exploratory owner QoS setting. Original/triple
wire rates improve from 118.52/117.88 to 154.09/152.95 KiB/s, about 30%, with
nearly unchanged whole-process CPU. This selects **20 us** for the
final combined build. There is one scored run per payload size and delay, not a
statistical bound or general scheduler-fairness guarantee. The final production
composition uses 20 us, restores the original owner QoS, and includes the
separately reviewed lazy error formatting change. A fresh independent build
repeats the triple payload in 89.42 seconds with exact bytes, a maximum heartbeat
gap of 1.04 seconds and clean S5 (98.29 seconds whole VM, 98.94 host CPU-seconds).

Eight additional 50-us focused VM runs pass: two QEMU references and four
natemu comparisons at normal/single-instruction block size preserve all UART
register, memory, exception and exact REP byte oracles; one normal REP stress
run records 892 mid-string deliveries and all 64 timer deliveries between
elements; one IRQ4 test completes 128 HLT instructions, 128 interrupts and 128
exact output bytes with no unexpected vector. HLT instruction/interrupt progress
does not directly observe host WFI residency. The 50-us build passes 189 host
unit tests with two ignored and 72 ABI tests, normal CLI/driver/runtime builds,
signing and resident stack checks. The 20-us rebuild repeats the host suite and
build/stack checks. Unit coverage includes polling and timer-only completion,
installed busy-guard acquisition, reset/loopback byte preservation, first sink
write/flush errors and concurrent idempotent shutdown drain.

All failures and frozen sources, commands, binaries, inputs and logs remain in
`natemu-raw/io-load-repair-20261108-01`. The author inventory
(`natemu-raw/io-load-repair-20261108-01/uart-author-summary-01.json`), tuning comparison
(`natemu-raw/io-load-repair-20261108-01/uart-tuning-analysis-01.json`) and common-oracle
rejudging
(`natemu-raw/io-load-repair-20261108-01/serial-oracle-05/existing-evidence-rejudge.json`) bind
the results. No kernel warning threshold or payload pacing changed in the candidate unpaced
runs.

The primary CPU-loaded extension runs the same triple unpaced payload while three workers from
the pinned stress-ng 0.19.02 binary each perform 180 seconds of verified `int64` work. It
samples actual worker PID, process group, start time and CPU ticks, requires consecutive
observations covering the whole output interval, and explicitly drains the guest console before
recording output completion. The first loaded attempt remains failed
(`natemu-raw/io-load-repair-20261108-01/final-loaded-serial-01/result.json`): although bytes,
heartbeat, worker verification and S5 checks passed, two worker observation sequences each
lacked one sample. The observer passed a changing `/proc/*/stat` glob as ordinary BusyBox awk
input; source inspection shows a vanished input can terminate that scan. An unrelated process
disappearing is a plausible cause, **not a confirmed retrospective explanation**, because that
attempt did not retain the failed scan's error/status.

The reviewed fixture correction uses nonfatal per-path `getline` reads. The
retained change (`natemu-raw/io-load-repair-20261108-01/serial-oracle-07/observer.patch`)
changes only the observer and its input checksum; kernel, BusyBox, stress-ng,
payload, init, worker recipe and strict consecutive-sample oracle are unchanged.
A synthetic check (`natemu-raw/io-load-repair-20261108-01/serial-oracle-07/selfcheck.log`)
places a vanished unrelated path before a stable worker and requires its row;
removing the actual worker still causes oracle rejection. The original failure
is not reclassified.

The fresh primary run (`natemu-raw/io-load-repair-20261108-01/final-loaded-serial-02/result.json`)
passes with the final combined build: exactly 14,144,544 wire bytes in **95.34
seconds**, maximum per-CPU heartbeat gap **1.15 seconds**, no kernel errors and
clean S5. Each worker has **92 consecutive observations**, from guest uptime
5.92 through 106.40 seconds, covering the drained output interval 8.91–104.25.
The workers report 327,450 / 326,976 / 324,750 verified operations over 180.00
seconds each, positive CPU progress, exit zero, one passed worker, zero failed
or skipped workers and exactly one zero untrustworthy-metrics summary. Whole-VM
time is 185.85 seconds and 610.93 host CPU-seconds. These costs include the full
180-second load, boot and export; they are not payload-only costs.

The fresh independent loaded replay
(`natemu-raw/io-load-repair-20261108-01/replica-serial-loaded-01/result.json`) also passes: the
same exact payload in 101.52 seconds, heartbeat gaps at most 1.15 seconds, and 96 consecutive
actual-worker observations spanning the drained output. Each worker completes verified
180-second work with zero failure/trust records; shutdown is clean. Whole-VM wall/CPU are
185.94/620.42 seconds. Together these establish the tested CPU-loaded serial behavior, not
arbitrary pressure or universal fairness. The original observer failure remains failed. Run
bindings (`natemu-raw/io-load-repair-20261108-01/final-loaded-serial-02/manifest.json`) and
fixture comparison (`natemu-raw/io-load-repair-20261108-01/serial-oracle-07/bindings.json`) bind
oracle `244d3f8b…`, input manifest `40d789dc…` and initramfs `4061cbac…` to the final binaries.
The independent replay binds the same fixture to its fresh build. Neither run establishes a
worst-case latency bound.

### Load-coordination diagnostic follow-up — November 8

The next investigation keeps the same N4 guest, two 64-MiB fio files and 60-second
unloaded/300-second loaded phases. Four bounded native thread samples in the normal
control-build diagnostic
(`natemu-raw/io-load-repair-20261108-01/baseline-profile-01-analysis.json`) find the I/O owner
inside copy-window coordination in 57–58% of its unloaded samples and 69–73% of its loaded
samples. Native disk-thread pread/pwrite samples are about 3–4% unloaded and 2% loaded. These
are sampled thread-stack proportions, not whole-process CPU attribution; resident guest
execution and resident waiting cannot be separated by those host samples alone.

An independently checked breakdown of those same samples finds 217/200 loaded
observations under the owner's kick path, including 194/174 inside
`hv_gic_set_spi`. Only 1/4 show natemu's own GIC mutex blocked outside that
framework call. Most sampled framework work is wake/trap activity. These are
inclusive stack observations, not CPU percentages or per-call timings; they do
not justify removing the lifetime/serialization lock. The
retained breakdown (`natemu-raw/io-load-repair-20261108-01/baseline-kick-sample-breakdown-01`)
instead motivates evaluating fewer coordination rounds through bounded queue
capture/completion batches. That remains a future design hypothesis with its own
ownership, reset, cancellation and fairness obligations.

An isolated source snapshot therefore adds cumulative host stage counters, per-thread CPU
observations and resident ACK-to-release elapsed counters. It keeps guest jobs, queue limits,
copy-first ownership and integrity oracles unchanged. The initial instrumented-profile-01
(`natemu-raw/io-load-repair-20261108-01/instrumented-profile-01/write/result.json`) is **failed
and retained**: the pre-existing 5-ms live perf reader exceeded the 32-MiB log cap after 217.8
seconds. There was no completed oracle or restart; zero errors in partial I/O counters do not
turn this into a pass. The corrected snapshot disables only that live reader, retaining final
owned vCPU records and new cumulative counters. Marker observation also switches from Python's
process-relative clock origin to the same native `clock_gettime_nsec_np(8)` used by the Rust
counters, with calibration retained per VM.

The corrected baseline and replayable analysis
(`natemu-raw/io-load-repair-20261108-01/instrumented-profile-01b-analysis-02/natemu-load-baseline-analysis.json`)
pass both the full soak and read-only restart. Interior host counter brackets cover 59.337
seconds unloaded and 299.468 seconds loaded:

| Average diagnostic cost | Unloaded | Loaded |
| --- | ---: | ---: |
| Whole copy window, µs | 5.073 | 13.271 |
| Kick/check stage per window, µs | 1.351 | 6.899 |
| ACK wait per window, µs | 3.421 | 5.709 |
| Post-ACK grant/invalidation/copy/release per window, µs | 0.134 | 0.312 |
| Native execution elapsed per job, µs | 2.705 | 5.803 |
| Owner wait for native lane per job, µs | 5.588 | 10.696 |

Resident ACK-to-release elapsed shares range from 14.0–21.3% per vCPU unloaded
to 24.2–33.2% loaded. They include descheduling and late private shadow-reset service,
exclude quiet-skipped epochs and work before ACK/after release, and are **not CPU
time**. The owner uses 0.645/0.632 CPU-seconds per elapsed second in its own
unloaded/loaded brackets; the disk lane uses 0.173/0.130. Each counter family has
its own bracket, and cumulative families are not transactional snapshots.
The 500-ms interior margins are relative to observed markers; observer scheduling
may exceed the nominal 250-ms polling cadence. Kick time includes participant
checks and barriers, not just native ringing. Stage and lane times overlap other
reported intervals and must not be added as independent CPU buckets. A guest
clock-watchdog read-back-delay/skipped-skew warning remains in the baseline log.

The source then tests only an interactive QoS request for the I/O owner, matching the existing
vCPU request. One-time queries confirm owner class 21→33 while all vCPUs remain 33 and the
native disk lane remains 21; lane inheritance must not be assumed. These are requested classes,
not effective priority or core placement. The QoS02 pair and complete comparison
(`natemu-raw/io-load-repair-20261108-01/instrumented-profile-02-analysis/comparison.json`) pass
the same integrity/restart checks but give **no adoption signal**: loaded window time is
13.271→13.296 µs, kick 6.899→6.961, ACK wait 5.709→5.697 and the whole post-ACK stage
0.312→0.312. Diagnostic read/write rates are 43.384/30.348→41.599/29.342 MiB/s. These two
sequential instrumented runs neither establish a statistical regression nor support a speedup;
the owner-QoS candidate is rejected for this increment. Independent review accepts source
isolation, clock/counter arithmetic, final invariants and this limited disposition.

A separate source-only patch
(`natemu-raw/io-load-repair-20261108-01/lazy-kick-error-01/change.patch`) changes
`Stops::kick_if_needed` from eager `ok_or(format!(...))` to `ok_or_else(|| format!(...))`. It
avoids formatting the invalid-index error on successful participant checks, preserving the error
text and synchronization. Its isolated gain is unmeasured; the completed normal-build,
independent and reverse-control results below do not attribute a benchmark improvement to it.
These diagnostics concern the current small-request profile and do not establish a universal
conclusion about copying, zero-copy designs or native networking.

### Completed normal combined comparison — November 8–9

Twelve normal VM runs pass: the initial control-build baseline (the earlier accepted
HLT-quiescence candidate, used unchanged), selected candidate and QEMU soak pairs and candidate
N4 ordinary-disk matrix, followed by an independent fresh-build candidate soak pair and a
reverse-order control pair. The selected final build
(`natemu-raw/io-load-repair-20261108-01/final-build-01/build-manifest.json`) contains the 20-us
UART repair and lazy error formatting, with the rejected owner QoS change removed. The
independent build (`natemu-raw/io-load-repair-20261108-01/replica-build-01/build-manifest.json`)
uses the same production source manifest. No diagnostic counters, perf sampler or test hooks run
here. All use frozen `inputs-03`, including the final guest console drain. The initial
baseline/candidate pass a later offline rejudge with oracle02; subsequent pairs use it directly.
Natemu's compile cache is disabled; backing-file host caching remains unchanged, with no host
cache purge.

The complete summary, analyzer and input bindings
(`natemu-raw/io-load-repair-20261108-01/final-normal-analysis-02/summary.json`) retain every
job's IOPS, completion p50/p95/p99, intervals, actual stress work, CPU time and warnings. The
reverse-order control follows the independent candidate because both candidate observations were
below the first control write rate; it uses the original control binaries and the same
workload/settings, without another change.

| Normal build / phase | Read, MiB/s | Write, MiB/s | Read p99, ms | Write p99, ms |
| --- | ---: | ---: | ---: | ---: |
| Control first, unloaded | 102.579 | 62.159 | 0.864 | 2.007 |
| Control reverse-order run, unloaded | 98.971 | 60.516 | 0.856 | 2.023 |
| Candidate author, unloaded | 102.504 | 57.641 | 0.889 | 2.474 |
| Candidate independent, unloaded | 103.671 | 59.242 | 0.881 | 2.408 |
| QEMU 11.1.2, unloaded | 82.475 | 74.320 | 0.823 | 0.963 |
| Control first, loaded | 44.403 | 34.077 | 5.997 | 7.438 |
| Control reverse-order run, loaded | 45.054 | 31.121 | 6.062 | 7.766 |
| Candidate author, loaded | 45.037 | 31.327 | 6.324 | 7.766 |
| Candidate independent, loaded | 43.827 | 31.772 | 6.259 | 7.635 |
| QEMU 11.1.2, loaded | 55.666 | 33.276 | 5.014 | 7.504 |

There is **no established throughput gain** from the combined change and no
isolated gain attributed to lazy formatting. The initial loaded write deficit
is not consistent against the reverse control: both candidate rates,
31.327–31.772 MiB/s, lie inside the two control observations' 31.121–34.077 MiB/s range.
That does not prove equal performance or rule out a smaller loaded cost.

The unloaded write deficit remains: candidate rates are 57.641–59.242 versus
The control build's 60.516–62.159 MiB/s, a 2.1–7.3% difference across these comparisons.
Candidate write p99 is also higher, 2.408–2.474 versus 2.007–2.023 ms. Both
candidate observations are below both control rate observations, so this remains an
unresolved small-write performance cost to investigate. These are observed ranges
from two controls and two candidates, not confidence intervals or an isolated
causal estimate; order was not randomized and no further timing runs are
included in this closed 40-VM campaign. The finite correctness acceptance does
not turn this remaining performance limitation into a speedup claim.

Each stress worker runs for about 360 seconds, but completes different work:

| Normal build | CPU0 operations | CPU1 operations | Memory operations | Summed worker guest CPU, s |
| --- | ---: | ---: | ---: | ---: |
| Control first | 331,658 | 335,672 | 49,061,888 | 934.59 |
| Control reverse-order run | 340,567 | 343,451 | 52,609,024 | 932.08 |
| Candidate author | 323,236 | 314,396 | 48,136,192 | 926.18 |
| Candidate independent | 335,995 | 334,593 | 49,815,552 | 932.36 |
| QEMU 11.1.2 | 116,382 | 119,668 | 36,069,376 | 885.74 |

The author candidate completes less stress work than the first control run;
the independent candidate and reverse control have different completed work
too. Equal duration must not be treated as equal CPU/memory load or used to
explain away the unloaded write deficit. QEMU completes much less CPU work
in the same wall interval, so its loaded I/O rates do not isolate emulation or
I/O overhead. MMIO/PCI, single-active-request/thread-pool and
`F_FULLFSYNC`/`fsync` differences also remain. These are whole-system comparisons
with warm host caching, not matched cold-storage or per-write stable-media rates.

| Normal build | Main VM wall / host CPU, s | Read-only restart wall / host CPU, s |
| --- | ---: | ---: |
| Control first | 483.21 / 1,791.73 | 11.25 / 13.28 |
| Control reverse-order run | 482.00 / 1,781.17 | 11.10 / 13.48 |
| Candidate author | 483.50 / 1,780.47 | 11.56 / 14.40 |
| Candidate independent | 481.81 / 1,783.97 | 11.42 / 14.31 |
| QEMU 11.1.2 | 477.42 / 1,688.84 | 15.09 / 14.25 |

Whole-process times include boot, integrity sweeps and UART evidence export;
they are not fio-only times. All five soak pairs have full CRC/hash verification,
clean S5, offline filesystem checks, unchanged read-only images and no kernel
error detected by the shared oracle. Raw job/worker logs and original verdicts
are retained. Image archival and build cleanup occur only between timed runs.
The last reverse-control image remains raw; earlier images have verified
lossless archives and restoration manifests.

The candidate's separate N4 matrix also passes all original profile, integrity,
filesystem and restart checks. It uses the matrix's larger files and job mix,
so its rates must not be substituted for the concurrent soak above:

| N4 ordinary-disk profile | Read, MiB/s | Write, MiB/s |
| --- | ---: | ---: |
| Sequential 4 KiB, Q16 | 142.616 | — |
| Sequential 128 KiB, Q16 | 2,448.825 | — |
| Sequential write 128 KiB, Q16 | — | 602.270 |
| Random read 4 KiB, Q1 | 56.669 | — |
| Random read 4 KiB, Q16 | 141.153 | — |
| Random read 4 KiB, Q64 | 140.774 | — |
| Four random-read jobs, 4 KiB, Q16 each, summed | 165.911 | — |
| Mixed 4 KiB, Q16 | 91.083 | 38.679 |
| 4-KiB writes with per-operation fsync, Q1 | — | 0.421 |

The matrix main/restart take 91.77/24.14 seconds and 182.55/37.31 host CPU-seconds.
The fsync row includes its synchronization cost; write completion latency alone
is not the flush latency. Full fio JSON retains the separate synchronization
statistics. This first matrix pair establishes the tested profiles' success,
not an independently replicated performance improvement.

Retained evidence stays recoverable within the campaign's storage bound. The closed-file
archival records (`natemu-raw/io-load-repair-20261108-01/reverse-closed-archive-01/result.json`)
preserve exact byte counts/SHA256 and restore commands for compressed logs and inactive serial
initramfs files, including failed attempts; restore their original paths before rerunning an
older analyzer or input check. Only rebuildable build intermediates were pruned, with
inventories and final executable hashes checked. The closed-input deduplication record
(`natemu-raw/io-load-repair-20261108-01/closed-blob-dedup-01/before.json`) preserves every
pinned byte/path/mode/owner for identical inactive blobs and explicitly records normalized host
mtimes and how to restore unique old mtimes. Frozen initramfs archives retain original guest
metadata. Active soak inputs, build sources and binaries were not changed by this cleanup.

### Strict stress-metric validation correction

During final-result inspection, the old checker was found to match
`metrics-untrustworthy:` while the installed stress-ng emits
`metrics untrustworthy:`. Its `all(...)` over absent matches could pass without
validating that summary. Other worker pass/fail/skip, progress and verification
checks remained active; this was a gap in the untrustworthy-metrics check.

The [checker correction](../../tools/run_io_soak.py) accepts either separator and requires
exactly one literal zero summary. Missing, duplicate, malformed and nonzero summaries are
rejected. The frozen oracle02 source manifest
(`natemu-raw/io-load-repair-20261108-01/final-soak-oracle-02/source-sha256.json`) changes only
this parser and its sample/self-check coverage; workload inputs are unchanged. Its retained
self-check passes 212 negative controls and two valid serial cases.

The supplementary audit
(`natemu-raw/io-load-repair-20261108-01/strict-worker-rejudge-01/result.json`) revalidates 21
actual worker logs from seven accepted pairs, all with zero untrustworthy metrics. For the prior
safety primary/independent pairs, normal sampled profile and two completed counter diagnostics,
the scope is strict worker-log validation only; it does not replace their historical whole-run
oracles. For the two final natemu soak pairs, the full current guest judge also passes both
phases offline. Original results, raw serial/exported-worker hashes and the new checker binding
are preserved. The incomplete capped diagnostic remains failed and is not promoted by this
audit. QEMU, the independent candidate and the reverse-order control subsequently pass oracle02
directly.

## Refactor replay and console drain — November 9

After the code was made idiomatic (same window protocol), the page-table mark became a
check before the atomic write, the unread window field was removed and window counters
were added, every November 8 I/O run was replayed with the new binaries from its saved
command. Raw results: `natemu-raw/io-replay-20261109-01/` and
`natemu-raw/uart-debug-20261109/`.

**Correctness.** Entropy at one, two and four vCPUs; block at one and two; ten of
eleven lifecycle cases; the network fixture; windows and window cases; quiet;
pending-compiler; normal four-vCPU and forced two-vCPU stress; reset and disable
lifecycle stress; the loaded serial stress (186.04 s against 186.20 s recorded); and the
fio admission write/relaunch pair all pass.

Three failures were traced, none to the refactor (each also fails at the
November 8 I/O commit):

- Block at four vCPUs and two matrix phases lost the end of their console output. Every
  failing run powered off while the guest's tty still held a few hundred bytes: the
  serial port's transmit-ready interrupt was raised and not yet serviced at power-off,
  and every byte the guest wrote reached the log. The block, entropy and fio guests
  powered off without draining the console, and the November 8 matrix inputs were
  prepared before the matrix guest gained its drain. The earlier instant-transmit
  serial model emptied the tty fast enough to hide this; a real 115200-baud port would
  lose the same output. With a TCSADRAIN drain: block 12 of 12 phases, matrix 8 of 8
  (two and four vCPUs), and the guest's kernel log shows the drain returning 0 after
  the remaining bytes were transmitted. One earlier instrumented run lost about 200
  bytes despite a drain on its failure path; it did not recur in ten later runs and is
  not explained.
- The lifecycle case "power-off with a halted peer" expected the peer to end
  cancelled with a nonzero halt count. Power drain now releases the disk's busy guard
  after its final sync, so the driver's halted rule may end the sleeping peer first,
  and a peer sleeping in its resident publishes no stats record before it ends. The
  old binary showed the same outcome about one time in eight. The judge now accepts
  either ending and still requires the peer-halted event, the RIP after HLT and the
  markers; six of six runs and all eleven cases pass.

**Benchmarks** (natemu only, two runs each; the QEMU side was not rerun). November 8 figure /
new runs. The one- and two-vCPU November 8 figures come from the matrix before the
halted-vCPU repair (that repair is why the two-vCPU reads rose); the four-vCPU figures are the
accepted post-repair candidates, "—" where none was recorded:

| Profile | N1 | N2 | N4 |
| --- | --- | --- | --- |
| random read 4 KiB, depth 1 (kIOPS) | 20.2 / 20.1, 19.9 | 12.3 / 15.0, 14.8 | — / 14.6, 14.6 |
| random read 4 KiB, depth 16 (kIOPS) | 27.2 / 28.4, 26.5 | 22.8 / 36.6, 35.7 | 36.6 / 35.0, 35.2 |
| four readers, depth 16 (kIOPS) | 27.0 / 26.3, 25.2 | 33.4 / 33.5, 33.5 | 46.3 / 44.1, 45.9 |
| mixed 70/30 (MiB/s) | 100 / 96, 94 | 126 / 127, 122 | — / 122.7, 107.8 |
| fsync (IOPS) | 106 / 109, 95 | 100 / 103, 98 | 102 / 121, 101 |

Sequential 128 KiB writes scatter as widely as before (485–1,319 MiB/s). Nothing moved
beyond recorded variation.

**Final HEAD (after the serde_json change)**, `natemu-raw/perf-head-20261109-01/`: the soak and
four matrix pairs (drained inputs) all pass, including fresh read-only verification. Soak
rates at four vCPUs (read job / write job, kIOPS), against the November 8 candidate and
baseline: unloaded 25.94 / 15.22 (26.53 / 14.98, 26.61 / 15.97); loaded with CPU and memory
workers 11.53 / 8.43 (11.55 / 8.05, 11.37 / 8.74). Matrix, two runs each at two and four
vCPUs: random read depth 1 14.9–15.0 and 14.5–14.9; depth 16 26.9 and 36.1 at two vCPUs (the
first run is low; its repeat is not), 34.3–36.4 at four; four readers 33.6–34.0 and
42.2–47.3; mixed 125–128 and 121–126 MiB/s; fsync 99–100 and 106–111 IOPS. All within the
variation recorded above.

**Window counters.** One 89-second four-vCPU write run (`--perf-log`) took 5,640,426
windows, 1,963,553 of them writing guest RAM, and 13 whose writes touched a page that had
held a page table. The never-cleared page-table marks are not yet a cost; long uptimes
remain unmeasured.

## Additional native VM references — November 8 suggestion

The owner suggested ARM Linux under Apple's Virtualization.framework and
[Apple Container](https://github.com/apple/container) as additional benchmark
references, without migrating natemu. Apple's Container tool runs Linux
containers in lightweight VMs using its
[Containerization package](https://github.com/apple/containerization).
Retain these as future comparison options, outside the current load/UART repair.

Use native ARM64 fio/stress-ng builds with matched job definitions, CPU/RAM
budgets and integrity checks. Record the actual disk attachment, filesystem,
container volume path, cache and flush policies, kernel, versions and host
resource use. A host-directory share must not silently stand in for a virtual
block disk. Report these as ARM-native reference systems: architectural and
deployment differences prevent interpreting the ratio as isolated natemu I/O
overhead. No Apple VM/container benchmark was run for this suggestion.

## Re-prepared inputs and fresh QEMU comparison — November 9

Inputs re-prepared with the drained guest scripts (`natemu-raw/io-inputs-20261109-01/`; block in
`block-drain-20261109-01/`). With HEAD (`natemu-raw/io-accept-20261109-01/`): entropy at one,
two and four vCPUs, block at four vCPUs, the fio admission write/relaunch pair and the matrix
pairs at two and four vCPUs for natemu and QEMU all pass, including fresh read-only persistence.
Same-day matrix, natemu / QEMU:

| Profile | 2 vCPUs | 4 vCPUs |
| --- | ---: | ---: |
| Random read 4 KiB Q1, kIOPS | 15.0 / 12.4 | 14.8 / 11.7 |
| Random read 4 KiB Q16, kIOPS | 28.1 / 16.2 | 36.0 / 16.5 |
| Four random readers Q16, kIOPS | 33.7 / 29.0 | 41.9 / 57.2 |
| Mixed 70/30 4 KiB Q16, MiB/s | 131.1 / 116.7 | 129.3 / 118.7 |
| Sequential read 128 KiB Q16, MiB/s | 2,494 / 157 | 2,718 / 159 |
| Sequential write 128 KiB Q16, MiB/s | 672 / 140 | 722 / 146 |

The four-reader case at four vCPUs remains natemu's one loss to QEMU (73%).

## Windowless copies — November 10

HEAD `816a84d` (stages 1–2 plus windowless copies with targeted invalidation), two matrix runs
per vCPU count, window counters and the soak (`natemu-raw/bench-windowless-20261110-01/`). All
phases pass, including fresh read-only verification.

| Profile, four vCPUs | Before stages 1–2 | Windowless | VZ reference |
| --- | ---: | ---: | ---: |
| Random read 4 KiB Q1, kIOPS | 14.5–14.9 | 15.4–19.3 | 33.2–33.8 |
| Random read 4 KiB Q16, kIOPS | 34.3–36.4 | 86.3–97.8 | 241.2–241.4 |
| Random read 4 KiB Q64, kIOPS | 30.2–36.2 | 63.5–96.6 | 258.1–258.5 |
| Four random readers Q16, kIOPS | 41.9–47.3 | 176.5–209.5 | 407.7–409.5 |
| Four readers, worst p99, ms | 6.3–10.7 | 0.9–1.4 | |
| Mixed 70/30 4 KiB Q16, MiB/s | 120.8–129.3 | 159.3–306.1 | 667.5–669.6 |
| Sequential read 128 KiB Q16, MiB/s | 1,991–2,718 | 2,936–4,533 | 7,218–7,259 |
| Sequential write 128 KiB Q16, MiB/s | 722–1,433 | 482–921 | 3,117–3,529 |

At one vCPU: depth 1 19.9–20.2, depth 16 49.9–50.2, four readers 47.4–47.5 kIOPS. At two:
depth 16 52.0–55.9, four readers 91.3–96.6. Soak at four vCPUs, read/write kIOPS: unloaded
87.26 / 60.97, loaded 49.67 / 27.59 (before: 25.94 / 15.22 and 11.53 / 8.43).

Windows per run fall from about 5 M to 7–12, all page-table fallbacks; total window time 1.5–2.4 ms
in 80–90 s, about 0.003% of wall time. The windowless gates (paused share below 2%, single-job
depth 16 at least 2x, depth 1 no worse) are met. The gap to Virtualization.framework narrows from
10–15% to roughly 40–50% for concurrent reads.

Sequential 128 KiB writes measured below the earlier pre-stage-2 range (482–921 against
722–1,433 MiB/s), but this is not a code regression: rerun the same day, the pre-stage-2 build
gives 507–817 and a one-worker variant of the current build 481–817 MiB/s
(`natemu-raw/seqwrite-20261110-01/`). The job ends with an `F_FULLFSYNC`, so its rate tracks APFS
flushing on a disk that has been 96–98% full since the earlier runs, under `fseventsd` load. The
same reruns keep the read gains (four readers 46 k on the old build, about 200 k now).

## Native and same-architecture virtualization references — November 9

The matrix's fio jobs (same precondition, nine profiles, CRC verify-all, four 256 MiB files)
were run on two ARM-native references at four CPUs, two runs each, under the VM lock:
`natemu-raw/native-refs-20261109-01/`.

- **VZ:** ARM64 Debian (pinned `ref.APPLE_IMAGE`) in Apple container 1.7.1, i.e.
  Virtualization.framework, kernel 6.18.56, 2 GiB; data on the container's own ext4 root, a
  virtio block disk (`/dev/vdb`), fio 3.39 (Debian), `io_uring`, `direct=1`.
- **Native:** macOS 26.6.2 on APFS, Homebrew fio 3.43, `posixaio`, `direct=1` (`F_NOCACHE`).

natemu is HEAD at four vCPUs (`natemu-raw/perf-head-20261109-01/`, two runs). Ranges:

| Profile | natemu | VZ | native macOS |
| --- | ---: | ---: | ---: |
| Sequential read 4 KiB Q16, MiB/s | 124–137 | 1,094–1,109 | 794–856 |
| Sequential read 128 KiB Q16, MiB/s | 1,991–2,381 | 7,218–7,259 | 2,865–3,310 |
| Sequential write 128 KiB Q16, MiB/s | 1,073–1,433 | 3,117–3,529 | 4,170–4,358 |
| Random read 4 KiB Q1, kIOPS | 14.5–14.9 | 33.2–33.8 | 11.0–13.6 |
| Random read 4 KiB Q16, kIOPS | 34.3–36.4 | 241.2–241.4 | 100.1–131.5 |
| Random read 4 KiB Q64, kIOPS | 30.2–34.9 | 258.1–258.5 | 100.6–134.3 |
| Four random readers Q16, kIOPS | 42.3–47.3 | 407.7–409.5 | 138.2–170.8 |
| Mixed 70/30 4 KiB Q16, MiB/s | 121–126 | 668–670 | 133–171 |
| 64 writes with fsync, job seconds | 0.58–0.60 | 0.01 | 0.01 |

Caveats. These are deployment references, not an isolated measure of natemu overhead:
natemu also translates x86 code, and the references run ARM-native kernels. natemu and VZ both
read a host file through the host page cache (natemu's `pread`, VZ's default disk caching), while
native `direct=1` bypasses it, which is why VZ exceeds native on reads. The fsync rows are not
comparable: natemu sends every flush through `F_FULLFSYNC`, macOS `fsync` and VZ's default
flush do not reach stable storage the same way.

Reading: natemu reaches about 40–45% of VZ at queue depth 1, 30% on large transfers, and only
10–15% at queue depth 16 or with several readers, where VZ keeps many requests in flight and
natemu serves one at a time while pausing every vCPU per window. The gap is in concurrency, not
per-request latency (Q1 p99 is 0.07 ms against VZ's 0.03 ms).

## Recommendation

Use **fio + iperf3 + the existing zstd build**, with **netperf TCP_RR** for the
latency question throughput cannot answer. Add one controlled HTTPS download to
the guest disk and a separate persistence check. Existing programs generate the
work; a small runner only prepares identical inputs, schedules overlap, collects
unmodified output and enforces bounds. Do not write a custom disk/network load
generator or collapse these independent results into one synthetic score.

There is no established single-suite result in this survey that automatically
proves natemu's disk+network+translated-CPU behavior. Concurrent execution of
existing tools is a proposed integration experiment, explicitly distinguished
from the upstream benchmark's own standard result.

| Tool | Relevant source-backed capability | Proposed role |
| --- | --- | --- |
| [fio][fio] | Configurable sequential/random/mixed I/O, depth, sync operations, latency reporting, JSON and data verification. Actual achieved depth depends on engine/OS; inspect its depth distribution. | Main storage tool inside the x86 guest, on a dedicated ext4 benchmark file. |
| [iperf3][iperf] | TCP/UDP, reverse and simultaneous bidirectional transfer; per-stream threads since3.16; JSON and server output. Its authors distinguish ordinary defaults from special maximum-performance options. | Guest→peer, peer→guest and simultaneous throughput. Keep one stream initially; four streams is a separately labelled scaling case. |
| [netperf][netperf] | TCP/UDP request-response tests and transaction timing/histogram support. Histogram instrumentation can itself affect timings. | TCP_RR small-request latency, alone and under I/O load. Report round-trip transactions, not inferred one-way latency. |
| [zstd1.5.7 build][zstd] | Existing upstream static-library Makefile target. Already used by natemu's [performance plan](perf-phase2-plan.md). | Preserve `make -jN libzstd.a`, gcc14.2/`-O0` and its existing recipe, now on persistent ext4. Full kernel compilation is too costly for the routine loop and is not proposed. |
| [fs_mark][fsmark] | File creation with explicit synchronization modes and per-operation timing. | Optional small-file/fsync extension if metadata becomes the decision; start with a bounded file count and explicit per-file sync mode. |
| [Filebench][filebench] | Existing workload models; upstream warns that bundled datasets are not sized appropriately for arbitrary machines. | Defer until a modeled filesystem workload adds information. The [fileserver profile][fileserver] performs filesystem operations; its name alone does not prove actual network traffic. |

An existing orchestration option is [Phoronix Test Suite/OpenBenchmarking profiles][pts].
It provides versioned profiles, input hashes and run recipes. Prefer it if the
selected profile exposes the required controls; pin its profile independently of
the harness version and disable automatic profile drift. It is optional for this
small matrix, not a reason to accept opaque defaults or install a large suite.

## Bounded initial matrix (historical)

Historical (November 8 planning; superseded by the measured sections above).
Kept as the original tool-selection plan; the cases below were proposals, and
the measured sections report what was actually run.

Workload clients execute in the guest, including the CPU work; network servers
run on the separately controlled peer. The following values
are a proposed starting workload definition, not queue/device tuning. Keep the
accepted128KiB request/segment limits, MTU1500, one RX/TX queue pair, no offloads
and one-request backend budgets unchanged. Application depth16 should exercise
backpressure even when the implementation serializes backend operations.

| Case | Fixed workload | Main reported result |
| --- | --- | --- |
| D1 | fio sequential128KiB read and write, depth16 | Bandwidth and latency; separate directions |
| D2 | fio random4KiB read, depth1 and16 | IOPS, achieved depth, median/p95/p99 latency |
| D3 | fio random4KiB70%read/30%write, depth1 and16 | Per-direction rates/latencies; mixed-load interference |
| D4 | fio buffered4KiB writes, synchronous engine, depth1, `fsync=1` | Durable-transaction wall time and sync latency, separate from buffered-write bandwidth |
| N1 | iperf3 TCP, one stream: forward, reverse, bidirectional | Receiver goodput, retransmits, endpoint CPU, each direction separately |
| N2 | iperf3 UDP1200-byte datagrams at two fixed offered rates | Achieved rate, loss and jitter; never report offered rate as delivered throughput |
| L1 | netperf TCP_RR, fixed64-byte request/response, one transaction outstanding | Transactions/s and available RTT distribution, idle then loaded |
| I1 | Existing clean zstd build on ext4, alone | Wall time, CPU time, output validation |
| I2 | Same build overlapping D3 depth16 and N1 bidirectional | Build slowdown plus disk/network throughput and tails during actual overlap |
| I3 | Controlled HTTPS object downloaded to ext4, then verify, full drain/shutdown/relaunch/reverify | Transfer time and separately durable end-to-end time; correctness result |

For fio use one pinned Linux async engine (prefer io_uring if admitted),
`direct=1` for D1–D3 and the same engine on both VMMs. Do not silently fall back
on one side. Use prewritten allocated data and distinct files for simultaneous
writers. Verification is a separate fixed-size write/read pass: time-based and
overlapping randomized writes need care; fio documents restrictions on verification
and async random-map choices. `end_fsync` and periodic sync are distinct options.
[fio manual][fio]

Choose dataset size before measuring: a concrete pilot can use an8GiB file if
the fixed guest has at most4GiB RAM and the raw image has sufficient space.
Otherwise revise both sides' dataset budget together; never shrink guest RAM to
manufacture a cache result. Precondition allocation and report host cache state:
this dataset rule does **not** promise that data bypasses the host cache.
Choose N2's low/pressure offered rates in an unscored endpoint/path pilot, freeze
the same rates for both VMMs, and retain the pilot; do not tune each side to its
best-looking loss point. Start N=2; N=1 is diagnostic and N=4 supplies SMP contrast.

I2 starts background disk/network work before the build, records readiness and
timestamps, and scores only the actual overlapping interval. A fast build does
not justify counting idle background seconds as integrated throughput. Repeat
the clean build a fixed predeclared number if necessary; never reuse prior object
files accidentally. The fio file and build tree occupy separate directories.
Run all components alone as controls; otherwise interference has no baseline.
Whole-run tool summaries cannot isolate this overlap. Retain timestamped
interval/operation logs or align measurement start/stop with the recorded overlap;
derive overlap-only latency tails from the corresponding retained samples or
histograms. Where the tool does not supply them, label its tails as whole-run
and restrict overlap claims to quantities actually resolved by its interval data.
Do not infer overlap percentiles by averaging whole-run/interval percentiles.

I3 uses a pinned nonzero object from a controlled endpoint with a known digest;
[curl][curl] provides output/error and transfer timing controls. Keep DNS/TLS
settings fixed, check exit status and actual bytes, and verify the stored digest.
Public mirrors/CDNs are compatibility demonstrations, not repeatable performance
endpoints. A download followed by a build is useful application coverage but
does not replace I2's simultaneous disk+network+CPU exercise.

## Cache, durability and topology are part of the result

**Three distinct caches:** guest filesystem cache, macOS backing-file cache and
natemu/QEMU translation caches. Guest `direct=1` does not make the backing image
uncached on macOS. Linux [drop_caches][cache] releases clean guest caches only;
it is not a host-cache purge. Preserve matched warm and cold-start categories,
state exactly what was reset, and do not call an unqualified run “cold disk.”
First image allocation, decompression, sparse-file expansion and initialization
are either included identically or measured as separate preparation.

**Durability:** record negotiated FLUSH, guest filesystem/mount settings and host
flush behavior. A guest fsync must traverse the implemented durability path;
guest direct I/O alone is not proof of stable storage. The [I/O plan](io-plan.md)
requires F_FULLFSYNC for the selected barrier/stable-write behavior. QEMU's
[documented cache modes][qemu] distinguish writeback, direct access and ignored
flushes; matching the word “writeback” alone does not establish equivalent Darwin
flush implementations. Inspect and record both paths. Never compare safe writes
against `cache=unsafe`, disabled barriers or discarded writes as equivalent.

**Topology:** guest↔host vmnet traffic measures a host-local virtual-network path;
guest↔external peer via vmnet adds its shared/NAT service and physical path.
Guest loopback bypasses virtio-net entirely. Label these separately. Use the same
controlled external peer, physical interface, MTU, address family and congestion
control for the main comparison. Record wired/Wi-Fi status and peer/link limits.
Keep peer CPU and traffic generators off the measured host for the external case;
calibrate that endpoint/path independently and retain saturated/limited outcomes.
If QEMU uses another backend, identify that difference: it is a whole-system
comparison, not isolation of translator or virtqueue efficiency.

## Controls against test-specific shortcuts

These are proposed measurement rules, aligned with Track05, not properties
automatically supplied by the selected tools:

- Freeze tool versions/build flags, job files, seeds, VM images, kernel, CPU/RAM,
  device masks, backend/queue limits, topology and correctness oracles before
  scored runs. Save exact commands and hashes. No benchmark-name/process/data
  detection, special completions, fake entropy, elided persistence or packet drops
  counted as successful work. Optimizations must preserve ordinary guest behavior.
- Retain a few alternate block sizes, random seeds and a four-stream network
  case as confirmation coverage; they are not used to tune the scored matrix.
  Do not set device budgets to fit a single request size or the benchmark's name.
- Keep ordinary iperf3 copying; no `--zerocopy`, `--skip-rx-copy`, special affinity
  or one-sided socket tuning in baseline. Such features may be measured as named,
  matched extensions, never silently mixed with default results. [iperf manual][iperf]
- Randomize paired natemu/QEMU order, use identical predeclared warmup and run
  lengths, record thermal/power/background state and retain every failure/timeout.
  No best-of-N, optional stopping at a favorable result or unreported exclusions.
  Clock elapsed work from the host as well as guest tools; qualify agreement
  before trusting guest-clock throughput. Report uncertainty, not bare averages.
- Report rates and tails per operation plus total host CPU across execution,
  translator, disk/packet workers and attributable native services. Include
  pause/wait time, queue occupancy, kicks/refaults and endpoint limits. Do not
  average percentiles across runs or call high throughput good if another
  component starves. Tool verification overhead is either retained identically
  or excluded from both scored paths with separate correctness runs.

**Correctness gate:** fixed-size verified writes, synchronized completion, clean
shutdown, fresh process launch and reread establish the planned persistence
scenario. Check transferred digests and build output too. Fault-injected short
I/O/flush errors, reset/stale completions and malformed descriptors remain device
tests; fio/iperf success cannot substitute for them. Clean restart is not a
power-loss or crash-consistency proof. Quarantine timings affected by a correctness
failure rather than accepting a fast invalid result.

## Next bounded step (historical)

Historical (November 8 planning; superseded by the measured sections above).
The matrix was later authorized and run; see the measured results.

After IO3/IO4 are runnable, pin binaries and existing zstd inputs; prepare exact
job files and an endpoint/image manifest. First use one exploratory30-second pass
per throughput case,5-second warmup and one integration pass at N=2, with a
90-minute total wall cap and explicit disk-space cap. This is a proposed bound,
not authorization or a run conducted here. A timeout is retained, not silently
replaced by an easier workload. Then review the decision-bearing matrix before
running it: start with five randomized paired repetitions,60-second throughput
intervals and N=2/4, adjusting the total run/storage budget in advance. If variance
or endpoint saturation prevents the desired conclusion, report that limit and
design the next bounded experiment; do not manufacture confidence from this count.

Independent source/design review accepted this shortlist and its compatibility
with the initial queue budgets, adding the overlap-measurement qualification
above. This is a reviewed research recommendation, not pre-execution acceptance
of a final benchmark harness or evidence from running the workloads.

[fio]: https://fio.readthedocs.io/en/latest/fio_doc.html
[iperf]: https://software.es.net/iperf/invoking.html
[netperf]: https://github.com/HewlettPackard/netperf/blob/master/doc/netperf.html
[zstd]: https://github.com/meta/zstd/blob/v1.5.7/lib/Makefile
[fsmark]: https://github.com/josefbacik/fs_mark
[filebench]: https://github.com/filebench/filebench
[fileserver]: https://github.com/filebench/filebench/blob/master/workloads/fileserver.f
[pts]: https://github.com/phoronix-test-suite/test-profiles
[curl]: https://curl.se/docs/manpage.html
[cache]: https://docs.kernel.org/6.16/admin-guide/sysctl/vm.html#drop-caches
[qemu]: https://www.qemu.org/docs/master/system/qemu-manpage.html

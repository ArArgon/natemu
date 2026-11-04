# Performance phase plan (draft)

Status: draft, November 3, 2026, HEAD 335c070 (series47-seg); revised after independent review; chunks 1-8 measured below (chunk 5 uncommitted on d89bc4c, chunk 7 on 0da552a, chunk 6 on ea11225, chunk 8 on 1c4d8b9). Scope: owner decisions of November 2/3 ([boot-features plan](boot-features-plan.md) §5): faster first-time
code, measured first; the correctness pass follows. Principles: measure first; native mechanisms; nothing keyed to
Linux (a KASLR boot must gain too); every object still admitted; checks green; no debt.

## 1. Starting point

From [boot-handoff.md](boot-handoff.md) §4 (Debian 6.12.107, 2 GiB, `nokaslr`, wall to the end-point line):

| Wall s | natemu warm | natemu cache off | QEMU multi | QEMU single |
| --- | --- | --- | --- | --- |
| Kernel only (root-fs panic), N = 1 | 2.28 | 4.15 | 1.96 | 1.92 |
| Kernel only, N = 4 | 2.31 | 5.70 | 1.65 | 1.86 |
| Installer ("Select a language"), N = 1 | 67.3 | 67.8 | 6.28 | 6.15 |
| Installer, N = 4 | 49.1 | 53.1 | 4.89 | 6.37 |

(QEMU columns: the like-for-like rerun of §5, `nokaslr`, replacing R2's 1.97/1.95, 1.70/1.86, 7.27/7.25, 5.46/7.62.)

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

### Measured (chunk 1, November 3; uncommitted on a73b858, series47-seg)

**Built.** Host: `crates/host/src/perf.rs` (`NATEMU_PERF_LOG=FILE` on `natemu-host boot-linux` and the `natemu` stub;
handoff §1 "Performance log"): process phase marks; per vCPU thread, every stretch of its service loop charged to a
named part (bookkeeping around each entry, `hv_vcpu_run`, the exit read, each request kind, the log's own line), the
request's parts (bytes copy, memory lookup, body + SHA-256 + pack read, fexc round trip, put, admission, decode, staging,
keeping the answer), thread CPU (`CLOCK_THREAD_CPUTIME_ID`), the fexc server's CPU from `wait4` after its stdin is
closed, and one line per request (CR3, RIP, guest-physical page, result, answer source, bytes read and their hash,
parts). Resident: `crates/runtime/src/perf.rs`, feature `perf`: every CNTVCT tick from the resident's start is charged to
one of 24 buckets (generated code; host exit; HLT park; mailbox wait; resident entries by kind from `returned`; inside
them, exclusively: `enter`, block lookup, exit patching, placement, system instruction, TLB flush, delivery,
revalidation walk, shadow-miss walk, fetch walk, shootdown wait, L1 clear), with entry counts; record at
`STATE_OFFSET + 0xe40`; an entry is charged to its kind from `returned`'s first instructions (the stop disarm and a
watch step included). Off, the resident's `.text` is byte-identical to a73b858's (only panic line numbers in `.rodata`
move); the host's off path is a test per lap and per request part. `boot-linux` now cancels on SIGINT/SIGTERM like the
stub (the report prints; both through `Canceller::on_signals`). After review: log write failures counted and reported,
1 GiB cap per request file, a warning when a `perf` resident runs with the log off, the server's `wait4` bounded (killed
after 1 s), the request line's byte hash moved out of the keep part (into the residual), and the entry kind charged from
`returned`'s start (before, the prelude and the stop disarm went to guest code). The table below was measured before
these changes; one instrumented installer boot at N = 1 cache off after them (`review/`) gives 68.34 s and every row
within 1-3% of the table's range (guest 4.04, enter 4.62, misses 4.57, linker 3.23, L1 2.99, fexc 35.6, unattributed
0.46%). Not built: fexc-side per-request stamps (they change the compiler binary, so its cache identity
and the standing compiler); `wait4` CPU and the sampling profile stand in (IPC and wake-up = round trip - fexc CPU).
Harness, analysis, summaries: `natemu-raw/perf1-20261103-01/` (`campaign.sh`, `boot.py`, `analyze.py`, `aggregate.py`,
`tables.md`, `tables.json`, `runs/*.summary.json`, `profile/`, `checks/`; per-request logs deleted after analysis).

**Method.** Debian 6.12.107, 2 GiB, `mitigations=off nokaslr` unless KASLR, `natemu-host boot-linux`, wall from spawn to
the end-point line (serial polled every 50 ms, as §5), one VM at a time, three interleaved rounds after a warm-up boot.
Instrumented runs use the `perf` resident and the log; uninstrumented runs the default resident, no log. Every part below
is measured; HVF entry and exit is `hv_vcpu_run` time minus the resident's in-VM ticks, and equals the resident's
host-exit bucket minus the host's own gap time to the millisecond in every run (same clock both sides). Acceptance met:
unattributed is at most 0.59% of the window for the process and every thread of all 24 instrumented runs.

| s (mean of 3 runs, instrumentation on) | Inst. N=1 warm | Inst. N=1 off | Inst. N=4 warm (vCPU 0) | Inst. N=4 off (vCPU 0) | Kernel N=1 warm | Kernel N=1 KASLR |
| --- | --- | --- | --- | --- | --- | --- |
| Process start to vCPU 0 entry (exec, `Boot::prepare`, VM/vCPU setup, compiler and persistent-cache open) | 0.13 | 0.07 | 0.19 | 0.06 | 0.17 | 0.07 |
| Host, fexc round trip (server start included) | 33.33 | 36.06 | 11.20 | 12.19 | 0.05 | 2.14 |
| Host, request body and persistent get (SHA-256 key, pack read) | 3.88 | 2.00 | 1.37 | 0.63 | 0.27 | 0.22 |
| Host, admission | 1.36 | 1.38 | 0.48 | 0.43 | 0.08 | 0.09 |
| Host, rest of the request (bytes copy, memory lookup, keep, staging, put, decode) | 2.01 | 1.38 | 0.74 | 0.43 | 0.06 | 0.11 |
| Host, exit/entry bookkeeping (watchdog and stop signalling, exit read, devices) | 1.31 | 0.95 | 0.55 | 0.42 | 0.14 | 0.15 |
| Host, the log's own request line | 0.40 | 0.40 | 0.15 | 0.11 | 0.02 | 0.03 |
| HVF entry and exit (run - in-VM) | 1.73 | 1.76 | 0.61 | 0.55 | 0.13 | 0.15 |
| VM, guest code (EL0) | 4.20 | 4.13 | 1.60 | 1.41 | 0.49 | 0.52 |
| VM, halted (HLT park) and AP mailbox wait | 0.00 | 0.00 | 25.99 | 31.17 | 0.00 | 0.00 |
| VM, entering blocks (`enter`) | 4.80 | 4.71 | 1.78 | 1.38 | 0.29 | 0.28 |
| VM, dispatcher misses (own: bank, events, L1 fill) | 4.66 | 4.57 | 1.77 | 1.21 | 0.04 | 0.05 |
| VM, linker entries (own: bank, `block_at`, checks) | 3.28 | 3.24 | 1.20 | 1.16 | 0.31 | 0.32 |
| VM, L1 clears at code-context changes | 3.07 | 3.02 | 1.17 | 0.69 | 0.00 | 0.00 |
| VM, block lookup, exit patching, placement | 1.09 | 1.06 | 0.36 | 0.33 | 0.07 | 0.07 |
| VM, address-space work: TLB flushes (CR3 loads), shadow misses (abort + walk), revalidation walks, #PF and other deliveries | 3.02 | 2.93 | 0.59 | 0.62 | 0.37 | 0.37 |
| VM, system instructions, fetch walks, IRQs, stops, helpers | 0.96 | 0.93 | 0.40 | 0.38 | 0.12 | 0.12 |
| Unattributed (loop end after its last lap; request time outside its parts) | 0.28 | 0.30 | 0.09 | 0.09 | 0.01 | 0.02 |
| **Sum = process start to vCPU 0's loop end** | **69.48** | **68.89** | **50.24** | **53.25** | **2.64** | **4.71** |
| Unattributed share, process / worst thread (worst of 3 runs) | 0.40% / 0.40% | 0.43% / 0.43% | 0.19% / 0.19% | 0.18% / 0.22% | 0.59% / 0.59% | 0.37% / 0.37% |
| Wall to the end point, on (off) | 69.47 (68.33) | 68.89 (68.48) | 50.23 (49.78) | 53.24 (53.94) | 2.62 (2.43) | 4.70 |

The loop end follows the end point by 0.01-0.02 s (the SIGINT cancel). Kernel N = 4 (2.50 s, KASLR 6.50 s) and every row
at full detail, the all-thread sums at N = 4 and the min-max spreads: `tables.md`. **The log's own cost** (on - off, mean of
3): installer +1.1 s (1.7%) warm and +0.4 s off at N = 1, +0.45 s and -0.7 s (inside the spread) at N = 4; kernel +0.19 s
(8%: about 1.2 M resident entries and 80 k request lines on 2.4 s).

**Findings** (installer at N = 1, cache off, unless named):

1. *First-time compiles*: 1,143,628 compiles, a fexc round trip of 31.5 us each (36.1 s, 52% of wall). fexc's CPU is
   31.2 s (27.3 us per compile), so pipes and wake-ups cost 4.8 s (4.2 us each). The sampling profile of one server (30 s)
   puts at least half of its request CPU in building the JSON report (52% under `Json::kv` and `Hex64` alone, more in
   string appends) and 18% in FEX's `CompileCode`; the host keeps no JSON for an object (`Reply::payload`, `Answer::new`). On the host every request costs
   about 4.1 us more: the request body's formatting 1.8 us (3.3 us with the SHA-256 key and pack read of a warm cache),
   admission 1.2, keeping the answer 0.5, bytes copy 0.5, staging 0.2.
2. *Reuse across load addresses*: 902,851 of the compiles (79%) read bytes that an earlier compile read at another RIP
   with the same page offset, 902,211 from the same guest-physical page; the run reads only 126 k distinct byte strings
   (44 k with a warm cache, whose 972 k compiles hold 902 k such repeats). 99% of them are user code (kernel compiles:
   180 k cache off, 9.5 k warm). A KASLR kernel with a warm cache compiles 61.5 k blocks of which 121 repeat bytes (4.70 s against 2.62 s, +2.1 s of
   compiles): KASLR picks the kernel's address once per boot, and the decompressor's relocations change the bytes that
   hold absolute addresses, so the persistent disk cache, written by an earlier boot at another address, misses.
   (Corrected November 3, chunk 5: within a run the kernel does not move, so the in-memory and in-VM caches work as with
   `nokaslr`; runtime text patching is independent of KASLR and handled by per-page invalidation. KASLR costs only the
   warm-cache speed-up, never the cold-boot comparison with QEMU.)
3. *The warm cache does not pay on the installer*: 171 k disk hits (4.2 us of get each) against 972 k misses that pay the
   SHA-256 key (+1.5 us) and a put; N = 1 warm 68.3 s, off 68.5 s uninstrumented.
4. *The resident's own work is about 20 s*, five times the guest's 4.1 s: 21.6 M block entries through `enter` (0.22 us
   each, 4.7 s), 14.7 M dispatcher misses (0.31 us own, 4.6 s), 6.9 M linker entries for 3.9 M links (0.47 us own,
   3.2 s), and 262 k L1 clears at 11.5 us each (3.0 s): L1 is cleared at every code-context change (`select_l1`), and the
   misses (56 per clear) refill it. The kernel-only boot clears it 4 times.
5. *Address-space work* is 2.9 s (4.3%): 30.6 k CR3 loads at 29 us each in the TLB flush (0.88 s; 4.4 us in the
   kernel-only boot: it grows with the cache), 2.94 M shadow misses 1.79 s, 2.99 M revalidation walks 0.17 s, 52 k #PF
   (69 k deliveries in all) 0.09 s. HVF entry and exit 1.76 s (1.24 M exits, 1.4 us each); exit/entry bookkeeping
   0.95 s (the watchdog channel's two sends per exit); 533 uncached steps (negligible).
6. *N = 4*: vCPU 0 is halted 26-31 s; the four threads together are halted 118 s of 213 s (off). 1.33 M compiles at
   37.3 us (four servers contend), 202 k of them (15%) of bytes at a RIP another vCPU had compiled; 49.6 s of round trips
   over the threads. Guest code is 5.6 s of thread time, under a quarter of wall: no guest-RIP sampling.
7. Process: natemu-host's `wait4` CPU 71.4 s (it appears to include its reaped servers: threads 37.7 + fexc 31.2);
   `DiskCache::open` 0.14-0.16 s of the warm start; the first exec after `codesign` costs about 0.23 s once.

**Candidates, by the bound these numbers give** (seconds of the N = 1 wall; they overlap: reuse removes compiles the
report change would shorten): host reuse across load addresses up to about 30 s (903 k compiles at 35.9 us becoming
memory hits at about 2-3 us; the relocation proof decides); fexc's per-object report about 16 s (52% of 31.2 s of fexc
CPU, from one profile: to be confirmed by measurement) plus up to 4.8 s of IPC; the resident's L1 clears and the entries
they cause up to about 11 s (3.0 s of clears, 4.6 s of misses, 0.4 s lookups, a share of the 4.7 s of `enter`); the
host's per-request work about 4.7 s; address-space switches 2.9 s; shared cache only at N > 1 (202 k compiles, about 7 s
of thread time at N = 4 off). **Recommended next**: the fexc report for objects (per-block overhead: §4 item 6's 5% bar
is met many times over; small, the objects must stay byte-identical) together with the relocation proof (§4 item 2),
then host reuse; the L1-clear finding goes to the owner's ranking with the resident-entry candidate (not one of §3's
named mechanisms).

**Checks** (`checks/`): `cargo test` 152 + 1 ignored without and with `FEXC` (one new test: `wait4` reaps the server with
its CPU time); `check_resident_stack.py` `ok` on the default resident (19,168 / 23,312 / 12,832, as before) and on the
`perf` one (19,184 / 23,328 / 12,848); with the instrumentation off and on (the runners' resident built with the feature
through `CARGO_TARGET_AARCH64_UNKNOWN_NONE_RUSTFLAGS` = the config's flags plus `--cfg feature="perf"`, into
`target/perf`; the stub's log on): `run_boot_smoke.py` 4/4 both, `run_slice1_faults.py --max-block 1` 7,912 match both,
`run_slice3_vm.py contain` 0 failures (423 controls) both; and all 40 measurement boots reached their end point. (A
first "on" pass set `RUSTFLAGS`, which replaces the config's linker flags: every run refused the resident ELF; rerun as
above.)

### Measured (chunk 2: fexc's report off the serve path, November 3; uncommitted on e9214c8, series48-lean)

**Built.** `tools/fexc/fexc.cpp`: the server (`fexc --serve`) no longer builds the JSON report. Its reply's JSON is what the
host reads (`Reply::payload`, `Answer::new`, `end_reason`, `system_cut`): `{"status":"ok"}` for an object; for no code the
status and `guest.end`, or, when FEX emitted nothing, the report without its header (status, error, `guest`, diagnostics).
`fexc --serve --report` keeps the full report (less the hex dumps, as before) for `test_serve.py`; the one-shot CLI is
unchanged. Everything the object and the cache key depend on was computed inside the report and now comes before it: the
zeroed relocation literal slots, the end marker and the end reason (with its two `InWindow` checks, so `extent` is the same).
`test_serve.py` also runs a server without `--report` beside the reporting one and requires the same code, object, extent and
args_read and only the host's fields in its JSON. Compiler **series48-lean** (`obj/fexc` SHA-256 `8c8bd659...fda4f592`,
reproduced by a fresh `build.sh`); handoff §6. Host unchanged.

**Identity.** The N = 1 cache-off installer boot's request stream on series47-seg, recorded by a `tee` in front of the server
(`natemu-raw/perf2-20261103-01/rec/fexc`): 1,142,279 requests (1,125,127 objects, 17,152 without code, no error), replayed in
order and in lockstep through a series47-seg server (full report) and a series48-lean one (`replay.py`): 0 differences in
code, object bytes, extent, args_read, status and every `guest.end` field (the host reads reason, RIP, `cut_opcode_map`,
`cut_opcode`, `cut_gated`, `cut_ud`); the objects' JSON was 8.13 GB of replies against 25.1 MB. `test_serve.py` (fixtures,
seed corpus, memory-form matrix, adversarial orders: 2,058 requests) identical in reuse and fork mode and against
series47-seg's CLI. The stream was deleted after the replay (about 2.8 GB; `rec/fexc` records it again).

**Measured** (method of chunk 1: `natemu-host boot-linux`, installer to "Select a language", 2 GiB, `mitigations=off
nokaslr`, one VM at a time; three interleaved rounds of every cell, old and new compiler each with its own warm cache after a
warm-up boot; log on = perf resident and `NATEMU_PERF_LOG`; mean, min-max; `tables2.md`, `tables2.json`, `runs/`):

| Installer | fexc | wall s, log off | wall s, log on | fexc round trip s (vCPU 0) | fexc CPU us / compile | round trip us / compile | compiles |
| --- | --- | --- | --- | --- | --- | --- | --- |
| N=1 warm | series47-seg | 68.00 (67.44-68.54) | 70.08 (69.40-70.72) | 33.28 | 30.3 | 34.5 | 0.964 M |
| N=1 warm | series48-lean | **52.15** (51.85-52.47) | 53.52 (53.37-53.71) | 17.67 | 14.4 | 18.3 | 0.965 M |
| N=1 off | series47-seg | 67.86 (67.09-68.30) | 69.34 (68.73-69.65) | 36.02 | 27.3 | 31.5 | 1.144 M |
| N=1 off | series48-lean | **49.32** (49.21-49.39) | 49.84 (49.79-49.93) | 18.61 | 12.7 | 16.3 | 1.143 M |
| N=4 warm | series47-seg | 49.82 (49.69-49.98) | 51.91 (50.77-54.07) | 10.77 | 33.6 | 39.3 | 1.082 M |
| N=4 warm | series48-lean | **37.55** (37.26-37.81) | 38.99 (38.27-40.01) | 5.57 | 16.3 | 21.7 | 1.085 M |
| N=4 off | series47-seg | 53.61 (52.85-54.04) | 53.86 (52.77-54.43) | 11.92 | 32.3 | 37.7 | 1.321 M |
| N=4 off | series48-lean | **37.56** (37.32-37.81) | 38.46 (38.15-38.84) | 7.48 | 15.4 | 20.7 | 1.324 M |

fexc CPU is the servers' `wait4` user + system over all threads' compiles; at N = 4 the per-compile figures are over all
four servers. Replayed alone through one server, the recorded stream costs 28.6 us of fexc CPU per request on series47-seg
and 12.3 us on series48-lean (two runs each, `replay-cpu.jsonl`). **Ship threshold met**: every cell is 12-19 s faster
(-23% to -30%), against run-to-run spreads of at most 1.2 s, and none is slower; the guest's "Run /init" stamp moves earlier
too (N = 1 off 7.09 to 5.13 s). Pipes and wake-ups (round trip - fexc CPU) stay about 3.6-5.7 us per compile. The warm cache
still does not pay at N = 1 (52.2 s against 49.3 s off: its SHA-256 key, get and put now outweigh a 16 us compile); at N = 4
warm and off are equal.

**Profile** (`profile/`: `sample` of the fexc server for 30 s, 20 s into an N = 1 cache-off boot): busy 5,997 of 23,504
samples (26%; series47-seg 10,394 of 23,579, 44%); the report's code (`Json::`, `Hex64`, its string appends) 103 samples
(4,870 or more before); FEX's `CompileCode` 2,398 (40% of the busy samples, 16% before). The rest is per-request fixed cost:
argument parsing and string compares (about 500), `FeatureTable` and its `std::function`s rebuilt per request (about 190),
the reply write (493), Config's rebuild (about 140), allocation: the next per-block-overhead candidate (§3), each at most a
few us per compile.

**Checks** (`checks/`, `suite/` on series48-lean, 2 GiB): `cargo test` 152 + 1 ignored, also with `FEXC` set; fexc
`test_slice1.py` 35, `test_fexc.py` 10, `check_sites.py --slice1` 64 programs (779 sites, 0 errors), `verify_precision.py`
PASS, `test_serve.py` (above); `run_slice1_vm.py` at both block sizes: fixtures 64, seed 44, I8 136, I8b 329 + the 7 known
QEMU two-NaN departures (the same 7 programs), I9 120, I9-big 5, I10 88, REX 37, 66-on-branches 12, I7 69; faults 7,912 and
the I7 corpus's 6,313 at both; ASM 1,875 / 0 fail / 48 / 347; `run_x1.py` 6/6 and 196/196; i10 faults 296 at both;
`run_i5c.py` 255 at both; `run_boot_smoke.py` 4/4; `run_r1.py r1a` 7/7, `r1b` 12/12 (negative controls flagged); all 52
measurement boots reached the dialog. Not rerun (no fexc change can reach them beyond the objects shown identical): the
rest of the suite.

### Measured (chunk 3: relocation proof, November 3; analysis on bc0c934, series48-lean, no code change)

**Question.** Can fexc's answer for bytes B at RIP r become the answer for B at r' (same page offset, half and arguments
but `--rip`) by its relocation records alone? **Answer: yes, with three refusals the relocator checks; no FEX change.**

**Method** (`natemu-raw/perf3-20261103-01/`: `tools/reloc_proof.py`, `proof.json` with up to 5 examples per class,
`proof.out`, `rec/`). One N = 1 cache-off installer boot recorded its request stream in front of a series48-lean server
(`rec/fexc`; 1,143,801 requests, 2.76 GB, deleted after the analysis). Every request was compiled again at its RIP (the
fresh answer). A request is a reuse candidate when an earlier answer has the same arguments but `--rip`, the same half
(bit 63) and page offset, and matches its bytes as `Entry::matches` does (bytes to the extent a prefix, or equal when the
extent reached their end). The candidate relocator turns the kept raw object into the object for r': `guest_rip := r'`;
every `guest_rip_literal` and `guest_rip_move` value + (r' - r); each move's words re-encoded at their offset by FEX's
`LoadConstant` (NOPAD, 64-bit; a Python copy, checked on all 2,235,105 moves of the fresh objects: 0 mismatches). The
result is compared byte for byte with the fresh object, and no-code answers by status, end reason, end RIP - RIP, the cut
fields, extent and args_read. Then each of the 20,785 answers met at two or more RIPs was compiled at 13 synthetic RIPs
of the same half and page offset (± 4 KiB, + 64 KiB, + 1.14 MiB, both sides of a 2 MiB and a 1 GiB boundary, bits 31:12
clear, bit 40 flipped, the half's edges, below 4 GiB, and for the kernel -2 GiB): 269,966 compiles. Every difference
was classified until none was left unexplained.

**Boot** (real pairs): 129,859 distinct answers; 914,403 requests (79.9%) met an answer kept at another RIP and 99,539
one kept at the same RIP (compiled again after the host's memory cache reset at 65,536 entries, `execution.rs:842`).

| Class (cause in FEX) | Boot pairs | Synthetic | Relocator |
| --- | --- | --- | --- |
| Object byte-identical after relocation | 898,394 | 201,481 | reuse |
| No code, equal (`system_instruction`, `partial_decode`) | 12,680 + 2,710 | 2,480 | reuse |
| Move length: `LoadConstant` picks movz/movk by the value's non-zero halfwords (also `orr` bitmask and `movn` forms); e.g. `0x7f94c6322000` (movz+movk+movk) at r' becomes `0x7f8faffa0000` (movz+movk) | 595 | 42,111 | refuses (word count) |
| 32-bit move form (`movzw+movkw`): the value is below 4 GiB. (Corrected after review: FEX's 32-bit `EntrypointOffset` mask, `ALUOps.cpp:56`, never applies, since slice1 is 64-bit only; the refusal is kept as a conservative rule that costs only code below 4 GiB) | 24 | 19,173 | refuses |
| Canonicality: a direct branch to a non-canonical target is cut at decode (`fexc_slice1.inc` 819-873, end reason `unsupported_form`): the block ends earlier (1,045), a move holds another exit (43), the first instruction gives no code (53); a window across the edge is an fexc error (57); plus 3,523 equal or move-length cases | 0 | 4,721 | refuses (non-canonical value) |

Every synthetic difference outside the 4 GiB next to a canonical edge is a move-length change or a 32-bit form; every canonicality case is
caught by the canonical rule. Not seen: a RIP constant folded without a relocation record, a site or PC-table entry, the
JITCodeTail (its RIP is a `guest_rip_literal`, zeroed), a branch-range choice (FEX's ADRP/ADR path is disabled; the code
holds no PC-relative reference to a guest address), split or alignment stubs (they depend on the low 12 bits only), extent
or args_read, a no-code answer's end. The relocator refuses 619 of the boot's 914,403 pairs (0.068%).

**Relocator** (next chunk, host). Inputs: a kept raw answer, its RIP r, the requested RIP r' (same arguments but `--rip`,
same half and page offset, bytes matching per `Entry::matches`). For an object, with d = r' - r: write `guest_rip` = r';
add d to the value of each reloc of kind 1 (literal; its slot stays zero, admission fills it) and kind 3 (move); for each
move, take its words by admission's `move_sequence`, encode the new value with `LoadConstant`'s rules, and write them.
Nothing else is touched (sites, PC table, kind 2 and 4 relocs, tail). It refuses (and the request is compiled at r') when
a move's word count changes, a move's old or new words are a 32-bit form, or a new value, r' or r' + `guest_bytes` is not
canonical. No-code answers are reused only for reasons decoded from the bytes (`system_instruction`, `partial_decode`,
`instruction_too_long`, `end_of_input`; the host's `system::decode` and `system_cut` stay exact: end RIP - RIP is the same),
never `unsupported_form` (its cut depends on the absolute target). **Keys**: in memory, context (limit, gating read),
half, page offset and a hash of the leading bytes, then `Entry::matches`; on disk, the SHA-256 of the body with the
`--rip` value replaced by half and offset. The relocated object goes through admission at r' as a fresh one does; an
admission refusal of a relocated object falls back to a compile, counted and logged (expected 0: every accepted
relocation above equals the fresh object byte for byte, and admission depends only on object, RIP and bytes); a fresh
object's refusal stays a host error. Under `--code-shape` each reused answer is also compiled and compared.

**Expected.** 913,784 of 1,143,801 compiles (79.9%) become relocated hits at N = 1 cache off. At series48-lean's 16.3 us
round trip against about 1 us of relocation, about 14 s of the 49.3 s wall (bound: all round trips, 18.6 s); a cache
that keeps its 129,859 answers instead of resetting adds the 99.5 k same-RIP repeats (about 1.5 s). No FEX change is
needed; a fixed four-word guest-RIP move would remove the 0.07% refusals at the cost of a word per move, not worth it.
KASLR is not a reuse problem within a run (the kernel stays at the address chosen at boot); across boots its relocated text
differs in bytes, so only the persistent cache's hits for the kernel are lost (chunk 1, corrected).

### Measured (chunk 4: reuse across load addresses, November 3; uncommitted on a24c785, series48-lean)

**Built** (host only; fexc and the resident unchanged). `crates/host/src/relocation.rs`: the relocator of chunk 3 with the
review's corrections: `object(raw, from, to)` writes `guest_rip`, adds `to - from` to every guest-RIP literal and move
value and re-encodes each move with FEX's `LoadConstant` rules (a Rust copy; its logical-immediate encoder checked against
the decoder on every encoding); it refuses an object whose end reason is `unsupported_form` (the reverse canonical case:
`add rax, rbx; jmp +0x7fffffff` is a 3-byte object at 0x7fffff000100 and an 8-byte one at 0x7ff0ff000100), a non-canonical
new value, RIP or block end, a 32-bit move form before or after (conservative), a move whose word count changes, an unknown
relocation kind and a malformed object. No-code answers are reused only for `system_instruction`, `partial_decode`,
`instruction_too_long` and `end_of_input`. `execution::Compiler`: the memory key is the block limit, the RIP's half and page
offset and up to 4 leading bytes the answer read, then `Entry::matches` (an entry kept for the requested RIP first); each
entry keeps its RIP; a hit at another RIP is relocated, then admitted at the real RIP like a fresh object; a refusal compiles
at the real RIP (`relocation_refused`; the disk is skipped then, it is keyed alike); a relocated object admission refuses is
logged and compiled (`relocation_fallbacks`). The wholesale reset at 65,536 entries is replaced by two generations of
262,144 answers or 128 MiB each, whichever comes first (a full newer generation becomes the older one; a hit in the older
moves to the newer): at most 256 MiB of kept answers per vCPU. Persistent cache:
key = SHA-256 of the body with the RIP replaced by `k:`/`u:` and the page offset, the payload prefixed by the RIP it was
compiled at, so a no-code answer's cut is derived at that RIP (`Answer::new(reply, kept rip)`); key version
`natemu-fexc-cache-2` (old packs never hit and age out). Debug check: `boot-linux --check-reuse`, and the program driver's
`--code-shape` (every suite runner passes it) or `--check-reuse`: each answer used at another RIP is also compiled there
and must be equal, else a host error. The performance log's vCPU line gains `relocated`, `relocation_refused`,
`relocation_fallbacks`, `reuse_checks`, `kept_bytes`, `kept_answers`. Tests: `relocation.rs` (fixtures
`crates/host/tests/relocation/`, made by series48-lean's CLI: relocation equals fexc's object at the other RIP both ways
and is admitted there, each refusal rule, the reverse canonical case, the encoders), and with `FEXC` the compiler reuses,
refuses and checks (`answers_are_reused_across_addresses`) and the persistent cache relocates.

**After review** (nothing blocking; fixed): the relocator itself refuses RIPs of another half or page offset (a forged
disk entry's RIP is only checksum-protected); the memory cache is bounded by bytes as well as answers (above); the check
also compares extent and args_read, and its compiles are counted as `reuse_checks`, not `compiles`; the lookup takes an
answer kept for the requested RIP in any bucket before relocating one; `natemu_abi::execution::bitmask` no longer overflows
on the reserved N = 0, imms = 63 in a debug build (it returns `None`); new tests: both refusals, the bitmask, and the
check's negative control (a kept object's guest-RIP literal tampered with: a host error under the check, a counted
admission fallback without it). Rerun (`natemu-raw/perf4-20261103-01/review/`): `cargo test` 159 + 1 ignored with `FEXC`
set; fixtures 64 at both block sizes; `run_k1.py` 192 cases, no problem; one installer boot (N = 1, cache off) with
`--check-reuse`: 920,414 reused answers compiled again and equal, 532 refused, 0 fallbacks, 126,918 compiles, 78 MB kept.
The measurements below are of the build before these fixes (none changes a hit or a miss on the boot but the lookup order).

**Proof additions** (`natemu-raw/perf3-20261103-01/`, review points): the tool's `normalise` masked four times the move's
words (it took a byte count as words; fixed: only the classification of a few cases could move, never an exact
equality); with the unsupported_form rule, a second recorded boot (1,144,252 requests, stream deleted) gives 914,714
pairs at another RIP: 898,419 objects and 15,392 no-code answers equal after relocation, 879 move-length and 24 32-bit
refusals, nothing else; every one of its 129,853 answers (not only the 20,762 met at two RIPs) compiled again at
+4 KiB, the next 2 MiB region and bit 36 flipped (389,297 compiles): 296,816 objects and 2,645 no-code answers equal,
80,099 move-length and 9,737 32-bit refusals, no other class (`proof2-all.json`).

**Check runs.** One installer boot (N = 1, cache off) with `--check-reuse`: 919,666 answers used at another RIP, each
compiled again and equal; 884 refused; 0 fallbacks (`natemu-raw/perf4-20261103-01/check/`). The standing suite with the
program driver's check on (`suite/`, 2 GiB, both block sizes): as chunk 2 (below), no check failure.

**Measured** (chunk 1's method; `natemu-host boot-linux`, installer to "Select a language", 2 GiB, `mitigations=off
nokaslr`, one VM at a time; three interleaved rounds per cell, before (a24c785) and after each with its own warm cache after
a warm-up boot; `natemu-raw/perf4-20261103-01/`: `campaign4.sh`, `rest4.sh` (the campaign's shell was stopped by a
30-minute limit after 43 of 48 boots; the last five ran from it), `boot.py`, `table4.py`, `tables4.json`, `runs/`;
counts summed over the vCPU threads of the log-on runs, means of 3):

| Installer | build | wall s, log off | wall s, log on | "Run /init" s | compiles | relocated | refused | fexc round trip s | admission s | kept answers / MB | max RSS MB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| N=1 warm | before | 52.03 (51.84-52.15) | 53.47 | 4.38 | 962.9 k | | | 17.75 | 1.43 | | 685 |
| N=1 warm | after | **26.18** (26.10-26.25) | 28.00 | 2.95 | 1.3 k | 971.7 k | 1,140 | 0.05 | 1.42 | 127.3 k / 78 | 648 |
| N=1 off | before | 49.15 (49.07-49.21) | 49.70 | 5.15 | 1,142.9 k | | | 18.57 | 1.27 | | 509 |
| N=1 off | after | **27.70** (27.35-27.91) | 28.91 | 4.00 | 128.4 k | 924.0 k | 2,413 | 2.18 | 1.33 | 128.4 k / 79 | 640 |
| N=4 warm | before | 37.46 (37.29-37.65) | 38.78 | 4.47 | 1,080.5 k | | | 23.43 | 1.70 | | 1,201 |
| N=4 warm | after | **18.83** (18.48-19.11) | 19.62 | 3.08 | 31.7 k | 1,048.4 k | 1,056 | 0.76 | 1.72 | 314.3 k / 194 | 1,235 |
| N=4 off | before | 38.32 (38.00-38.80) | 38.24 | 6.41 | 1,315.4 k | | | 27.07 | 1.72 | | 1,040 |
| N=4 off | after | **22.84** (22.65-22.95) | 23.33 | 5.43 | 318.5 k | 942.6 k | 1,495 | 6.71 | 1.78 | 318.5 k / 196 | 1,233 |

**Ship threshold met**: every cell is 15.5-25.9 s faster (-40% to -50%) against spreads of at most 0.8 s; none is slower.
Achieved at N = 1 cache off: 924.0 k relocated hits, 80.8% of the before build's compiles; compiles fall 88.8% (the
generations also keep the 99.5 k same-RIP repeats the reset lost); 0 admission fallbacks in all 24 log-on runs. Memory:
the kept answers are about 79 MB per vCPU (127-128 k answers, about 650 bytes each with their bytes); max RSS +131 MB at N = 1
off and +193 MB at N = 4 off; warm -37 MB at N = 1, +34 MB at N = 4. At N = 4 each vCPU still keeps
and compiles its own (the shared cache candidate, below): 318.5 k compiles of 1.26 M requests. The warm cache now pays:
26.2 s against 27.7 s off at N = 1 (its hits are relocated too: 1.3 k compiles).

**Checks**: `cargo test` 157 + 1 ignored, also with `FEXC` set (5 new); the standing suite parts of chunk 2 on the new build
(`suite/`): `run_slice1_vm.py` at both block sizes: fixtures 64, seed 44, I8 136, I8b 329 + the 7 known QEMU two-NaN
departures, I9 120, I9-big 5, I10 88, REX 37, 66-on-branches 12, I7 69; faults 7,912 and the I7 corpus's 6,313 at both;
ASM 1,875 / 0 fail / 48 / 347; `run_x1.py` 6/6 and 196/196; i10 faults 296 at both; `run_i5c.py` 255 at both;
`run_boot_smoke.py` 4/4; `run_r1.py r1a` 7/7, `r1b` 12/12 (negative controls flagged); all 51 boots (50 measured, 1 check) reached the dialog.
Not rerun: fexc's own tests (fexc unchanged) and the suite's other runners.

### Measured (chunk 5: one L1 table per code context, November 3; uncommitted on d89bc4c, series48-lean)

**Breakdown on d89bc4c** (chunk 1's method and scripts, `natemu-raw/perf5-20261103-01/`: `campaign5.sh`, `boot.py`,
`analyze.py`, `aggregate.py`; installer to "Select a language", 2 GiB, `mitigations=off nokaslr`, cache off, one VM at a time,
three rounds after a warm-up boot; `step1/` alone, `step3/` interleaved with the fix; s of vCPU 0's window, mean of 3, log on):

| s, vCPU 0 (log on) | N=1 d89bc4c (step1) | N=1 d89bc4c (step3) | N=1 **after** | N=4 d89bc4c (step1) | N=4 d89bc4c (step3) | N=4 **after** |
| --- | --- | --- | --- | --- | --- | --- |
| Host, fexc round trip | 2.16 | 2.13 | 2.14 | 1.86 | 1.98 | 1.91 |
| Host, admission / rest of the request / bookkeeping / log line | 1.28 / 1.00 / 0.82 / 0.45 | 1.33 / 1.01 / 0.84 / 0.46 | 1.36 / 1.03 / 0.87 / 0.51 | 0.45 / 0.34 / 0.41 / 0.16 | 0.43 / 0.33 / 0.40 / 0.15 | 0.51 / 0.39 / 0.47 / 0.19 |
| HVF entry and exit | 1.22 | 1.25 | 1.29 | 0.44 | 0.43 | 0.50 |
| VM, guest code | 3.51 | 3.56 | 2.32 | 1.21 | 1.00 | 0.90 |
| VM, halted and mailbox | 0 | 0 | 0 | 12.62 | 13.45 | 8.13 |
| VM, entering blocks (`enter`) | 4.10 | 4.15 | 1.87 | 1.28 | 1.08 | 0.69 |
| VM, dispatcher misses (own) | 4.00 | 4.07 | 0.89 | 1.17 | 0.93 | 0.29 |
| VM, linker entries (own) | 2.62 | 2.66 | 2.40 | 0.97 | 0.93 | 0.96 |
| VM, L1 at code-context changes | 2.76 | 2.80 | **0.02** | 0.72 | 0.57 | **0.01** |
| VM, block lookup, exit patching, placement | 0.90 | 0.93 | 0.76 | 0.31 | 0.29 | 0.28 |
| VM, address-space work (TLB flushes, shadow misses, aborts, revalidation, deliveries) | 2.12 | 2.16 | 2.01 | 0.54 | 0.53 | 0.55 |
| VM, system instructions, fetch walks, IRQs, helpers | 0.79 | 0.80 | 0.84 | 0.32 | 0.30 | 0.35 |
| Unattributed | 0.49 | 0.49 | 0.49 | 0.26 | 0.27 | 0.27 |
| **Window (process start to vCPU 0's loop end)** | **28.25** | **28.68** | **18.83** | **23.09** | **23.08** | **16.42** |
| Dispatcher misses / block entries / linker entries, all threads (M) | 13.08 / 19.35 / 6.20 | 13.10 / 19.40 / 6.22 | 2.57 / 8.29 / 5.66 | 13.33 / 19.40 / 5.95 | 13.28 / 19.36 / 5.97 | 2.62 / 8.41 / 5.69 |

Unattributed is at most 2.64% of the window (process and every thread, all 18 log-on runs). With reuse (chunk 4) the
compiles are 128 k at N = 1 (2.1 s of round trips): the resident's own work is now the largest share, and of it L1 clears and
the misses and entries refilling L1 were about 11 s.

**Cause** (a temporary diagnostic, `diag/diag.patch`, removed: in `select_l1`, the changed context bits, the last
cause before the change and the contexts seen; one boot each, `diag/`): at N = 1, 244,980 changes, every one but 4 at
start-up between exactly two context words, 0x22804 (CPL 0) and 0x22807 (CPL 3): bits 0-1 (CPL) only. Causes: SYSCALL
67,343, SYSRET 66,460, IRETQ 56,028, exception deliveries from CPL 3 51,967 (#PF mostly), other events 3,178; CR0.TS, MXCSR,
FCW, CR4, XCR0 never change after start-up (Linux 6.12 has no lazy FPU; MXCSR and FCW stay at their masked defaults). vCPU
0 at N = 4: 89,050, the same two words. Each change emptied L1's 4,096 entries (11.5 us: a word per entry, then cache
maintenance of 128 KiB), because L1 is keyed by RIP alone: the kernel's and user code's entries never collide by RIP, but
exactness (K1: a block never runs under other gating than it was compiled for, e.g. a CPL 0 block entered at CPL 3) needs the
context in the key, and no L1 entry carried it.

**Built** (resident; `crates/abi/src/execution.rs` cache layout, `crates/runtime/src/cache.rs`). L1 is four tables
(`L1_TABLES`, 128 KiB each after the dispatcher; the head grows by 384 KiB of the 128 MiB code area; their owners fill the
lookup area's existing 64 KiB), each tagged with the context word its entries were filled under (`Control::l1_contexts`,
`NO_CONTEXT` for none). The dispatcher is unchanged (`and; adr; add; br`, no data read: no hot-path cost); `select_l1`
switches tables by rewriting its `adr` word (one word and its cache maintenance), to the table tagged with the new context,
or, if none is, to the least recently left table cleared first. Every table stays exact: a CR3 load scans every tagged
table's owners as it scanned L1's (blocks no longer current leave); an invalidation or INVLPG (`cut_one`) empties a block's
entry in every table; a global flush and a reset clear the dispatcher's table and untag the others (cleared before reuse);
`fill_l1` enters a block only into a table of its own context. `fill_head` checks that the template's `adr` is the encoding
`select_l1` writes (else `UNSUPPORTED_MODE`). The K1 statistics gain `l1_switches` and `l1_clears`; the perf `l1` bucket
times the switches. After an independent review (no blocking defect): `reset` keeps the dispatcher's table tagged (one
clear, not two), `fill_l1`'s tag check, the test's switch/clear assertion; the measurements are of the build before these
(off the boot's path but for one clear at start-up).

**Test** (`tools/run_k1.py contexts`, natemu only, 1-4 vCPUs, both block sizes): one block called through the dispatcher
(`mov rax, V; jmp rax`, its `jmp r13` back) K = 50 rounds of A, C, A, C (two tables kept and switched between), B, D, E, F:
A MXCSR 0x1f80 runs, C (PM clear) takes #XM, B (CR0.TS) #NM, D/E/F (UM, ZM, DM clear) run; six contexts for four tables, so
tables are evicted and reused. Expected per CPU: 251 runs, 50 #NM, 100 #XM, and fewer L1 clears than switches less K; green.
Negative controls (`k1-contexts-nc1/`, `-nc2/`): no switch (the table re-tagged in place) gives 401 runs, 0 #NM, 0 #XM; an
evicted table reused uncleared 299 runs, 1 #NM, 101 #XM: both red. After the coordinator's review (no CR3 load, INVLPG or code
write had reached a table other than the dispatcher's in any K1 case), the case ends with V's block kept in A's table and,
under C, V remapped to another frame with INVLPG, that frame's bytes rewritten, V remapped again with a CR3 reload (no
INVLPG); each followed by a call under A that must run the new bytes (2, 3, 4). Green at 1-4 vCPUs and both block sizes;
red with `cut_one` limited to the dispatcher's table (1, 1, 4; with `--max-block 1` the stale block triple-faults), with the
CR3-load scan so limited (2, 3, 3) and with both (1, 1, 1) (`k1-kept*/`). `fill_head` also checks that every table's `adr`
decodes to that table.

**Measured** (`step3/`, three interleaved rounds before and after, log off; mean, min-max):

| Installer, cache off | d89bc4c | after | change |
| --- | --- | --- | --- |
| N = 1 wall s | 27.65 (27.61-27.71) | **17.38** (17.27-17.45) | -10.27 s (-37%) |
| N = 4 wall s | 22.59 (22.49-22.67) | **15.56** (15.50-15.66) | -7.03 s (-31%) |
| N = 1 / N = 4, log on | 28.68 / 23.08 | 18.82 / 16.41 | |
| "Run /init" guest s, N = 1 / 4 | 4.04 / 5.38 | 3.57 / 5.05 | |
| L1 clears per boot, N = 1 (vCPU 0) | 244.7 k | 21 (240.5 k switches) | |

**Ship threshold met**: -7.0 and -10.3 s against spreads of at most 0.2 s. Beyond the clears, misses fall 13.1 M to
2.6 M and block entries 19.4 M to 8.3 M (each L1 refill was a miss and a resident entry); guest code also runs 1.2 s faster at
N = 1 (likely: no 128 KiB of L1 code rewritten and evicted from the caches at every system call; not measured). A CR3 load's scan covers two tagged tables
(`tlb_us` 0.61 to 0.65 s at N = 1).

**Largest resident cost left** (N = 1, vCPU 0): linker entries 2.40 s (5.66 M for 3.15 M links; 3.13 M links are undone when their targets stop being current (CR3 loads, INVLPG, #PF;
the split: chunk 8) or die, and are made again after the revalidation walk), guest
code 2.32 s, `enter` 1.87 s (8.3 M at 0.23 us), the host's fexc 2.14 s and admission 1.36 s, address-space work 2.0 s.
Proposed next: keep links into a block across a CR3 load while the block is revalidated unchanged (an epoch per address
space, as the "Address-space switches" candidate keeps shadows per CR3 root), bounded by the linker's 2.4 s plus a share of
`enter`; then the "Resident entry cost" items (about 0.23 us per entry).

**Whole-cache flushes** (counted after the measurements above: `Control::flushes`, `flush_ticks`, `full`, `peak`, in the K1
statistics as `flushes`, `flush_us`, `full`, `used`, `peak`, `capacity`; two boots at N = 1 and two at N = 4, cache off,
log on, `flush/`; not fixed in this chunk). Every capacity flush is the **metadata arena** filling (64 MiB): at N = 1
4 capacity flushes per boot (and one more of another reason, 5 in all), at N = 4 one per vCPU (2 in all). At each, the arena
is at 64 MiB with about 254-255 k blocks (about 263 bytes of record and sites per block), the block index at 97% of its
262,144, the code area at 40.5-41.0 MiB of 128 MiB (32%). The flush itself is cheap (19-21 ms per boot at N = 1, 8-11 ms per
vCPU at N = 4); its cost is the refill: at N = 1 the boot makes 1.16 M translation requests for 127 k compiles, and only about
43 k of the 1.06 M memory hits follow a kill or a remap (39 k blocks killed, 4 k remapped), so about 1.0 M (4 x 255 k) are
blocks placed again after a flush, each a host exit and a memory hit (about 2.1 us of request plus about 1.4 us of exit and
bookkeeping) and a placement, linking and L1 fill again: about 3.5-4.5 s of the 17.4 s at N = 1 (an estimate from the per-request
means, not measured directly); at N = 4 230-310 k memory hits per vCPU. (Corrected November 3, chunk 6, measured: only about 123 k of the
1.18 M placed requests at N = 1 are at a RIP placed before the last flush; about 1.03 M are at a RIP never asked before,
0.91 M of them memory hits relocated from another address (chunk 4). The refill bounds at about 0.4-0.5 s, not 3.5-4.5 s.) Candidates: a larger arena and index with L2 to match
(the code area is two thirds empty), smaller records (sites dominate), or evicting part of the cache instead of all of it.

**Checks** (`suite/`: the handoff §5 block, every command at 2 GiB, both block sizes, on the build with the review's
changes; `final/` after the flush counters): `cargo test` 159 + 1 ignored, also with `FEXC` set; `check_resident_stack.py`
`ok` (19,168 / 23,312 / 12,832, as before); Python 317; everything green but the 7 known i8b QEMU two-NaN departures at both
block sizes (the same 7 programs): fixtures 64, seed 44, faults 7,912 at both, ASM 1,875 / 0 / 48 / 347, `run_k1.py` 200
cases with `contexts` (none failing, counts unchanged by rounds), `run_k2.py`, every `run_slice3_vm.py` mode (`contain` 423
controls), `run_x1.py` 6/6 and 196/196, `run_i5c.py` 255, `run_r1.py r1a` 7/7 and `r1b` 12/12 (negative controls flagged),
`run_boot_smoke.py` 4/4, and the rest of the block with exit 0. On the final build: `cargo test` both ways, the stack check,
`run_k1.py` (200 cases, with the extended `contexts`), `run_k2.py`, fixtures at both block sizes, `run_boot_smoke.py` 4/4; all 33 installer boots of `step1/`, `step3/`
and `flush/` reached the dialog.

### Measured (chunk 6: whole-cache flushes, November 3; uncommitted on ea11225, series48-lean)

**Question 1, the refill** (`natemu-raw/perf6-20261103-02/`: `campaign6.sh`, `boot.py`, `refill6.py`, `table6.py`, `runs/`;
installer to "Select a language", 2 GiB, `mitigations=off nokaslr`, cache off, one VM at a time, a warm-up boot each, three
interleaved rounds per build; the request line now carries the resident's flush generation, the code context, a template bit
and the second fetch page, handoff §1). Placed requests (blocks and system decodes) by RIP, mean of three log-on runs, all
vCPUs: at N = 1, 1,176.9 k, of which 1,033.2 k at a RIP never asked before on that vCPU, **123.1 k (10.5%) at a RIP whose
block was placed before the last flush** (0.18 s of host request time) and 20.6 k at a RIP asked earlier in the same
generation (killed, remapped); at N = 4, 1,363.9 k: 1,253.9 k new, 86.8 k before a flush, 23.3 k same generation. Every
capacity flush is the metadata arena (4 per boot at N = 1, one per vCPU at N = 4; `full` [0, 4, 0]). Chunk 5's "about 1.0 M
placed again" was wrong (corrected there): most of those are first placements at new linear addresses, of which 913.5 k
were memory hits relocated by chunk 4's reuse. The refill bounds at about 0.4-0.5 s of 16.0 s (123 k x about 3 us of request,
exit and placement), under §4's 5% bar.

**Question 2, reuse inside the resident** (`refill6.py`; the host marks a block a template when it is not a step, has no
address or halving cut, its answer read neither to its bytes' end nor beyond the block's pages, and does not end
`unsupported_form`: 99.6% of blocks). Block requests whose guest-physical page, page offset, context, bytes read (length and
hash; and the second page when the bytes cross into it) a template placed earlier on the same vCPU, in the same generation, at
another RIP had: **870.2 k at N = 1** (887.4 k with one flush), 900.4 k at N = 4 over the four vCPUs. This is an upper bound:
a template killed by a write whose page later holds the same bytes again still counts (39 k blocks are killed per boot), and
the design's direct-mapped hint table is not modelled. Above the design's 0.5 M: reuse inside the resident is worth building.

**Tried: right-sizing the arena and index** (the inherited partial patch, built and measured; not shipped). Per block the
installer averages about 160 bytes of code and 263 of record: the 152-byte `Block` (64 of them four inline `LinkPoint`s of 16)
and about 2.8 access sites of 40 bytes (FEX's site records, copied as admitted). A 256 MiB arena after the writable alias, a
1 Mi block index and L2 of 2 Mi entries (+192 MiB and +24 MiB of address space per vCPU) leave the code area as the limit: at
N = 1 one flush at 844 k blocks (127 MiB of code, 211 MiB of records), 31.1 k refills instead of 123.1 k. Log off, mean
(min-max) of three: N = 1 **16.43 s (16.08-16.81) against 16.04 s (15.75-16.32)** before, no faster (host request time
-0.23 s, `hv_vcpu_run` +0.28 s on vCPU 0, likely the first touches of the larger areas; not measured further); max RSS 871 MiB
against 610 MiB. At N = 4 all six boots hung: vCPU 3's window lay past IPA + 1 GiB, outside the 1 GiB identity alias `_start`
enables the MMU through (host `memory::private_space`), so it faulted at its first instruction and never came up. A
compile-time check now holds every vCPU's window inside that alias (`abi execution.rs`, after `BACKING_BYTES`; it rejects
the patch's layout). **Not shipped**: no gain beyond the spread, +261 MiB at N = 1. Not built either: smaller records (link
points out of line, compact sites: perhaps 100 bytes of 263; memory only, since capacity measured no gain) and incremental
(generational or 2Q) eviction, bounded by the same 0.4-0.5 s. **Memory budget unchanged**: per vCPU a 64 MiB arena, 256 Ki
blocks, up to 128 MiB of code and its lookup area (L2 8 MiB).

**Built.** Host performance log only (`perf.rs`, `execution.rs`): the four request-line fields above. `abi`: the window
check. `run_slice2_probes.py`: two cases with more blocks than the records hold, at the largest code cache, two trips each:
300,000 one-instruction ADDs fill the block index (`full` [0, 0, 2], 600,007 requests for 300,003 RIPs, 3 flushes) and
810,000 `add rax, [rsi]` in blocks of three (three sites each) fill the arena first (`full` [0, 2, 0], peak 246,777 blocks);
both match their x86-derived results. Negative controls (`cap/`): the index bound raised by 4,096 makes the index case a host
error (index entries overwrite link records); the arena bound raised by 1 MiB makes the arena case a resident abort.

**Checks** (`suite/`, `suite6.sh`, on the shipped tree, series48-lean, 2 GiB): `cargo test` 159 + 1 ignored, also with `FEXC`
set; `check_resident_stack.py` `ok` (depths unchanged); Python 317; fixtures 64 and faults 7,912 at both block sizes;
`run_slice2_probes.py` 85 match (with the two new cases); `run_k1.py` 200 cases, no problem; `run_k2.py` all ok; `contain` 0
failures (423 controls); `run_boot_smoke.py` 4/4; `run_r1.py r1a` 7/7 and `r1b` 12/12 (negative controls flagged); all 18
before-build boots and the after build's 6 at N = 1 reached the dialog. Not rerun: the rest of the standing suite (the resident
is unchanged). Per-request logs deleted after analysis (also the inherited `perf6-20261103-01/refill/`).

### Measured (chunk 7: host per-request cleanups, November 3; uncommitted on 0da552a, series48-lean)

**Built** (host only; fexc and the resident unchanged; the study is the per-request host work at 0da552a, chunk 5's
breakdown). `execution::run`: the watchdog no longer gets a channel send at every entry and exit (each woke its thread:
a cross-core wake twice per exit). The vCPU stores its entry time (`perf::now`) into an atomic before `hv_vcpu_run` and 0
after; the watchdog thread sleeps until that entry's deadline (entry + `watchdog`) and, if the same entry is still in the
guest and the vCPU is not halted, forces it out and repeats every 100 ms, as before (halted: it waits `watchdog`, then
100 ms after a first kick, again, as before); in the host it looks again after `watchdog` (at least 1 ms); the run's end
drops a channel sender, which wakes and ends it. `Requests::rips` (a set of every block RIP, about 1 M entries on the
installer) is kept only when the caller asks (`Some`: the program driver, which reports it). The guest bytes are copied
with volatile 8-byte loads where aligned into one buffer, not a zeroed `Vec` per page and a volatile byte loop
(`copy_out`, checked against `GuestRam::copy_out` at every alignment). The memory cache: one pass over the leading-byte
lengths (each bucket of each generation looked up once, an answer kept for the requested RIP still first), the entry
looked up once in `reuse`, and its hash is Thorup's strongly universal multiply-shift for vectors (128-bit keys from a
`RandomState` per map) instead of SipHash: the keys are guest-chosen (bytes, page offsets), but the guest never sees a
hash, so they collide no more than by chance (no quadratic buckets). Performance log: a part `relocate` (relocating an
answer kept for another RIP), so `admit` is admission alone (in the vCPU line; the request line keeps its nine parts).
Every object still goes through full admission. Not changed (measured small): the x87 argument `Vec` and the breakpoint
`Vec` (empty, so no allocation, on the boots), the `State` record's copies (the whole exit read is 0.08 s at N = 1).

**Measured** (chunk 1's method and scripts, `natemu-raw/perf7-20261103-01/`: `campaign7.sh`, `runs/`, `table7.txt`;
installer to "Select a language", 2 GiB, `mitigations=off nokaslr`, cache off, one VM at a time, a warm-up boot each, three
interleaved rounds before (0da552a) and after; mean, min-max; parts in s, log on):

| Installer, cache off | N = 1 before | N = 1 after | N = 4 before | N = 4 after |
| --- | --- | --- | --- | --- |
| Wall s, log off | 17.76 (17.60-17.83) | **15.85** (15.72-15.96) | 15.68 (15.43-15.96) | **13.56** (13.45-13.63) |
| Wall s, log on | 19.03 | 17.01 | 16.61 | 14.28 |
| "Run /init" guest s (log off) | 3.62 | 3.29 | 5.00 | 4.51 |
| Exit bookkeeping (`signal`), all threads | 0.855 | 0.042 | 1.475 | 0.060 |
| Bytes copy (`fetch`), all threads | 0.466 | 0.200 | 0.589 | 0.274 |
| Memory lookup, all threads | 0.386 | 0.235 | 0.488 | 0.293 |
| Staging (the RIP set was inside it), all threads | 0.147 | 0.016 | 0.172 | 0.021 |
| Admission (before: with relocation) / relocation, all threads | 1.323 / - | 0.970 / 0.249 | 1.785 / - | 1.270 / 0.285 |
| vCPU 0 `hv_vcpu_run` (in VM and HVF) | 12.49 | 12.15 | 12.74 | 11.56 |

**Ship threshold met**: -1.91 s (-11%) at N = 1 and -2.12 s (-14%) at N = 4 against spreads of at most 0.5 s; nothing slower;
all 26 boots reached the dialog; unattributed at most 2.6% of the window (process and every thread). Beyond the host parts
(about 1.5 s at N = 1), the guest side gains too (N = 4 vCPU 0's time in `hv_vcpu_run` -1.2 s; likely: no watchdog wake-ups
competing for cores; not measured). Max RSS -30 MB at N = 1 (the RIP set).

**Admission split** (log on, all threads, mean of 3): at N = 1, 1,160.8 k admissions take 0.970 s (0.84 us each) and
928.7 k relocations 0.249 s (0.27 us each; 787 refused); at N = 4, 1,339.5 k admissions 1.270 s (0.95 us) and 946.1 k
relocations 0.285 s (0.30 us). A relocated hit therefore costs about 1.1 us of host work, of which its full admission is
about 0.84: the relocated-hit re-check (study 1b: only the RIP-dependent rules on an answer whose admission is kept)
bounds at about 0.78 s at N = 1 (0.93 M x 0.84 us) plus part of the relocation's copies; not built (every object is still
admitted in full).

**Checks** (`natemu-raw/perf7-20261103-01/suite/`, the scratch copy with this change, series48-lean, 2 GiB): `cargo test` 159 + 1 ignored, also with `FEXC` set (the
fetch test extended to the word copy); `run_boot_smoke.py` 4/4; `run_slice3_vm.py contain` 0 failures (423 controls; its
watchdog cases: a 300 ms watchdog stops a spinning vCPU at 0.32 s, unacked shootdowns at 1.02 s);
`run_slice1_faults.py` 7,912 match (default block size); `run_slice2_probes.py` 74 match (`spin --watchdog-ms 300` stopped
by the watchdog); `run_x3.py` 9/9, 4/4, 24/24, 6/6, 10/10, 54/54; `run_x4.py` 33/33, 28/28, 6/6; `run_l4a.py` 13/13, 4/4,
2/2, 2/2, 1/1; `run_l4b.py` 5/5, 2/2, 3/3, 11/11, 7/7, 1/1. Measured on the build before one change (the watchdog's
in-host poll floored at 1 ms, which only a watchdog under 1 ms reaches); the checks ran on the final build. After the independent
review (nothing blocking): `run_l4a.py halt` gains the halt program with a 20 ms watchdog, shorter than its 60 ms halt
(the halted vCPU must reach its end, not be forced out): green, 3/3; red with the watchdog kicking halted vCPUs too (cpu0
stopped `watchdog`, no delivery; `natemu-raw/perf7-20261103-01/review/`). With `--check-reuse`, the check's fexc round trip
is in `fexc` and its comparison in no part (it was inside `admit` before). Not rerun: the rest of the standing suite.

### Measured (chunk 8: links kept across TLB events, November 3; uncommitted on 1c4d8b9, series48-lean)

**Cause first** (a temporary diagnostic, `natemu-raw/perf8-20261103-01/diag/diag.patch`, removed: each undone link counted
by the event that undid it and classified; one boot each, cache off, log off). At N = 1 3.03 M links are undone:

| Event | Undone | Target CPL 0 | Target new since placement | Of them placed from global pages (A) | Target on the source's pages, same frames (B) | INVLPG: target off the named 4 KiB page, no large leaf (C) | Any of A-C |
| --- | --- | --- | --- | --- | --- | --- | --- |
| CR3 load (23.2 k) | 2,300 k | 136 k | 699 k | 134 k | 1,890 k | - | 1,911 k |
| Global flush (78) | 121 k | 119 k | 17 k | 0 | 88 k | - | 88 k |
| INVLPG (80.1 k) | 497 k | 0 | 320 k | 0 | 428 k | 497 k | 497 k |
| #PF (52.6 k) | 0 | | | | | | |
| Death (code write) / flush | 19 k / 96 k | | | | | | |

At N = 4 (vCPU 0) the same shape: CR3 347 k (B 286 k, A 61 k), INVLPG 154 k (all C), global 109 k. The prune over every
record at each event scanned 185 M records in 0.60 s (most of `tlb_us` 0.62 s); INVLPG's 2 MiB scan cost 0.44 s. User
code is most of it: a CR3 load undoes the links of every non-global block, though most of them jump within one page of one
frame; an INVLPG (a COW, an munmap) cut every block of the 2 MiB around it.

**Built** (resident; `crates/abi/src/execution.rs`, `crates/runtime/src/cache.rs`, `execution.rs`; host: two K1 fields).
- *A, global at placement.* The placement's fetch walk is a TLB fill: its leaves' G and size go to `Control::fetched`;
  a block or system record is placed `GLOBAL` when every source leaf is global and CR4.PGE is set (`leaves`, as revalidation
  computes it) and `LARGE` when a leaf is 2 MiB (both recomputed at each revalidation). Before, a new block was never
  global, so each new kernel block lost its links at the next CR3 load.
- *B, local links.* A link from S into T is *local* when every linear page of T is a page of S translated from the same
  guest-physical page (`cache::local`). It stays while T is not current: S runs only under a translation of its pages that
  is valid now (entered current by lookup, L1 or a link into a current block, or by a local link from a block that was:
  induction), and T fetches only those pages through the same frames in the same context. The frame check matters: after a
  PTE change with no invalidation S and T may come from different frames at one linear page in one epoch.
  The link records are three contiguous segments (`Control::links_cr3`, `links_tlb`, `links`): links from another page into
  non-global targets, into global ones, and local ones (`insert` and `remove` move at most one record per later segment). A
  target's `GLOBAL` cannot change while a cross link into it exists (links go only to current targets; `GLOBAL` is rewritten
  only when a non-current block is revalidated). So a CR3 load prunes the first segment only, a global flush, INVLPG and #PF
  the first two, and a death all of them when the dead block has link points or `LOCAL_INBOUND` (set by a local link, never
  cleared but by a flush), else the first two if `INBOUND`. A local link's source shares every page of its target, so it
  dies with it; the flag is a second guard. Local links outlive their targets' currency, so the 64 Ki records filled
  (a first build stalled at 65,536 links: 150 s, never at the dialog); when full, the local links into blocks not current are
  undone first (`reclaims` in the K1 statistics: count, links). 256 Ki records were no faster (13.45 against 13.42 s, one
  pair of boots: the 4-5 capacity flushes undo every link anyway); the area stays 64 Ki.
- *C, INVLPG by page* (`cache::invlpg`, also for #PF): the blocks with bytes on the named 4 KiB page, and `LARGE` ones with
  bytes in its 2 MiB region (`Control::large_regions`, as `regions`), stop being current, global or not (SDM Vol.3 4.10.4.1:
  the entries for the page, all of them for a page larger than 4 KiB). A 4 KiB entry of another page is no entry for this
  page, so other blocks keep theirs. Data shadows still drop the 2 MiB (unchanged). The #PF case gains the large-page rule.

**Exactness.** K1's rule holds: a block runs only under a translation valid now (A: the placement walk is the check B and
revalidation already rely on; B: the induction above; C: the SDM's INVLPG reach). Every kept link is still undone when its
target dies or its page is written, on every vCPU: links are per vCPU, each vCPU's death of a page (its own write, a peer's
post drained at its next entry) prunes every segment. Tests (`tools/run_k1.py`, at 1-4 vCPUs and block sizes default and 1;
natemu-only cases assert natemu's choice where x86 allows either): `place-global` (a global page remapped with no INVLPG and
a CR3 reload keeps the old frame; a block crossing into a non-global page, and one placed with PGE off, must not),
`invlpg-page` (INVLPG of V leaves three linked calls of N, another page of its region, on N's old frame), `invlpg-large`
(INVLPG of another 4 KiB address of a 2 MiB page reaches its code: a load and the call see the new frame; QEMU 11.1.2 loads
the new data but runs the old block, so the case is natemu-only), `cross-link` (QEMU: a direct jump into an instruction
crossing into a page remapped with INVLPG of that page alone), `local-gpa` (S's link into T made after a remap with no
INVLPG, so from another frame; back with a CR3 reload, S must reach the old frame's T), `local-write` (QEMU: R -> S -> T from
one jump site, S global; INVLPG, calls, CR3 reload, then CPU (i + 1) % N, or itself at N = 1, rewrites T: the calls run the new
bytes). 48 runs green. Each red under its mutation (scratch residents, `k1-mut/`): placement not global (`place-global` r9 2),
global from the first page alone (rsi 0x100), PGE ignored (rbp 3); INVLPG over the 2 MiB (`invlpg-page` r9 6), the large rule
dropped (`invlpg-large` r9 1); local without the frame check (`local-gpa` r10 2), with the first page only (`cross-link` r9
0x300 for 0x600); a death pruning only the first segment (`local-write` r10 3 for 9: the stale bytes ran, at N = 1, 2, 4,
where a peer wrote), links into dead blocks kept (a `bad_link` stop). Not testable by a mutation: dropping `LOCAL_INBOUND`
(the co-dying source always forces the full pass).

**Measured** (`natemu-raw/perf8-20261103-01/`; installer to "Select a language", 2 GiB, `mitigations=off nokaslr`, cache
off, one VM at a time; mean, min-max). The record: `runs2/` (`one.sh`), log off, three interleaved rounds at N = 1 before
(1c4d8b9) and after, then one boot each at N = 4, with the machine idle (checked for other VMs and QEMU after each boot):
N = 1 **15.56 (15.27-15.84) to 13.55 (13.51-13.58) s**, N = 4 **13.39 to 12.42 s**. The parts below are from the full
campaign before it (`campaign8.sh`, `table8.py`, `table8.txt`, `tables8.json`, `runs/`: chunk 1's method, a warm-up boot each,
three interleaved rounds, log on and off at N = 1 and 4): another session's QEMU probe overlapped one of its boots for up to
about 2 minutes (not seen by the campaign's poll, which looked for `natemu-host` only), so its walls are indicative only:

| Installer, cache off | N = 1 before | N = 1 after | N = 4 before | N = 4 after |
| --- | --- | --- | --- | --- |
| Wall s, log off | 16.90 (16.82-16.98) | **14.64** (14.58-14.67) | 14.21 (14.12-14.34) | **13.33** (13.23-13.50) |
| Wall s, log on | 17.87 | 15.46 | 15.11 | 14.17 |
| "Run /init" guest s | 3.51 | 3.36 | 4.76 | 4.73 |
| Links made / undone / reclaimed, all threads (M) | 3.15 / 3.13 / - | 1.50 / 0.71 / 0.75 | 3.05 / 2.97 / - | 1.69 / 0.67 / 0.84 |
| Linker entries / block entries, all threads (M) | 5.64 / 8.27 | 3.95 / 6.45 | 5.65 / 8.35 | 4.27 / 6.87 |
| Revalidations / made not current by INVLPG or #PF (M) | 2.18 / 1.01 | 0.63 / 0.05 | 1.89 / 0.97 | 0.59 / 0.03 |
| vCPU 0: linker entries / `enter` / TLB flushes / revalidation s | 2.41 / 1.94 / 0.69 / 0.13 | 1.83 / 1.31 / 0.29 / 0.04 | 0.91 / 0.68 / 0.16 / 0.05 | 0.79 / 0.50 / 0.05 / 0.01 |
| INVLPG time, all threads s | 0.47 | 0.04 | 0.64 | 0.04 |

**Ship threshold met** on the record: -2.01 s (-13%) at N = 1 against spreads of at most 0.57 s, -0.97 s (-7%) at N = 4 (one
boot each; the campaign's three rounds -0.88 s, spreads 0.3 s); nothing slower; all boots reached the dialog. Prune, timed in a diagnostic boot of the new build (N = 1): 23 k CR3 prunes 46 ms,
2.8 k TLB-scope 38 ms, 1.8 k deaths 31 ms, 18 reclaims 91 ms, against 0.60 s before. The links still made are mostly first
links of new blocks (1.16 M placed) and the reclaimed ones; the rest of the address-space work is the shadows (unchanged).
Memory unchanged.

**Independent review** (read-only, of the diff): no blocking defect; it traced `insert`, `remove` and `prune` for every
segment (records stay contiguous in `[0, links)`, which the host's link check reads), the `GLOBAL` invariant, local-link
soundness over every entry path (L1, lookup, cross and local links, the step block, breakpoints, `disarm`, REP self-links,
system records) and INVLPG/#PF. Applied: `invlpg-page` now calls N through one jump site, so the second loop takes the link
kept from the first (before, each call site ran once and the case showed only that N stayed current); a comment.
Noted, not changed: `large_regions` bits stay set until a reset and alias modulo 1024 regions, as `regions` do (an INVLPG
there scans the region's 512 chains, as every INVLPG did before).

**Checks** (`suite/`, `suite8.sh`, on the shipped tree, series48-lean, 2 GiB): `cargo test` 159 + 1 ignored, also with `FEXC`
set; `check_resident_stack.py` `ok` (19,184 / 23,328 / 12,848 bytes: +16 each, within the budget); fixtures 64 and seed 44 at
both block sizes; faults 7,912 at both; `run_slice3_vm.py` `contain` 0 failures (423 controls), `smp`, `sharedpt`,
`coherence` 0 failures; `run_k1.py` 248 runs (the six new programs included), no failure, counts 14 and 14 requests at K and
2K rounds; `run_k2.py`, `run_m1.py` 20 cases, `run_s4.py` (QEMU's two known aborts on `s4-d-stack` recorded as departures, as
before), `run_x1.py` 6/6 and 196/196, `run_boot_smoke.py` 4/4, `run_r1.py r1a` 7/7 and `r1b` 12/12 (negative controls
flagged), all 0 failures; every measurement boot reached the dialog. The reworked `invlpg-page`: green at 1-4 vCPUs and both
block sizes, red under the 2 MiB mutation (r9 6). Not rerun: the rest of the standing suite (the change is in the resident's
link and TLB bookkeeping, which these exercise).

## 3. Candidate chunks

Each ships only if wall improves beyond the spread of three runs with nothing slower, and ends with the standing suite
at both RAM sizes and the boots of §5.

**Translations reusable across load addresses (host).** *Hypothesis:* most compiles are the same bytes at another
page-aligned base (shared libraries, PIE; a KASLR kernel only across boots, through the persistent cache). *RIP dependence today:* the memory key `(rip, limit)`
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
difference is a fexc patch before reuse ships (done, chunk 3: no patch; the relocator refuses three classes; built in chunk 4). Under the suites' code-shape option each reused answer is also compiled
fresh and compared. *Expected:* about 1 M compiles become hits that still cost a host round trip (4-6 us): roughly 25
of the 35 s. KASLR: the kernel's address is fixed for the run, so in-run reuse is as with `nokaslr`; the decompressor's
relocations change bytes holding absolute addresses, so only the persistent cache across boots misses for the kernel (the
warm-cache speed-up, not the cold boot). *Decides:* reuse bound, proof differences, fallback rate.

**Reuse inside the resident.** *Hypothesis:* about 1 M exits then remain because the resident looks up by linear RIP
(plan, Built (K1)) though the physical page is translated at another base. *Mechanism:* on a miss the reverse map
(`PAGE_BLOCKS`) finds a block of that physical page, offset and context; with the relocation list admission gave it,
the resident copies it, applies the shift, checks canonicality and move fit, else asks the host. Only immediates of
words admission classified as RIP moves or literal slots change: no branch, exit or access is created. This is
admission-equivalent code and gets its own review. *Expected:* up to 1 M x 4-6 us. *Decides:* exits left after host
reuse (chunk 6: at most 870 k requests at N = 1 and 900 k at N = 4 find a live template, above the design study's 0.5 M bar).

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
`fexc.cpp:463`; the JSON report the host dropped for objects: removed in chunk 2; argument parsing and the feature table
per request, chunk 2's profile); IPC by pipes and `poll` (564-611), a
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
4. Host reuse across addresses, if the proof is clean (done: chunk 4).
5. Breakdown again; then reuse inside the resident, translating and placing ahead of use.
6. Address-space switches, per-block overhead, resident entry cost, uncached RAM: only where the latest breakdown bounds
   the saving at 5% or more of a boot's wall.

If the unexplained 28-33 s is guest execution, halted time or address-space work, its chunk goes before 5. Each chunk
has an independent reviewer.

## 5. Comparison with QEMU and records

Same end points and harness as the handoff §4's like-for-like table (wall from process start to the end-point line, serial
polled every 50 ms, nothing else polled on either side; guest printk time; QEMU `-nodefaults` with natemu's devices and the
closest CPU model TCG boots; the aggregator fails on a boot that missed its end point); QEMU 11.1.2 `thread=single` and `thread=multi`; N = 1 and 4 (2, 3 for kernel
only); both emulators with and without `nokaslr`; natemu cache warm and off; three runs, one VM at a time. Raw data in
`natemu-raw/perf-YYYYMMDD-NN/` (`boots.jsonl`, serial and perf logs, profiles, commit, fexc SHA-256). Each chunk's
"Built" note holds before/after tables; the handoff's boot tables and INV-03-01's "Next phase" row are updated. Claims
beyond development results get pre-execution review and replication.

**Measured like for like** (integrity audit, November 3; `natemu-raw/qcmp-20261103-01/`, natemu at 1c4d8b9 plus the audit's
change, series48-lean; method and the one QEMU boot that missed its end point in the handoff §4):

| To the root-fs panic: wall s mean (min-max; guest s) | N = 1 | N = 2 | N = 3 | N = 4 |
| --- | --- | --- | --- | --- |
| natemu `boot-linux`, cache warm, `nokaslr` | 2.25 (2.21-2.32; 1.10) | 2.06 (1.83-2.52; 1.38) | 1.90 (1.89-1.91; 1.25) | 2.01 (2.00-2.02; 1.34) |
| natemu `boot-linux`, cache off, `nokaslr` | 3.27 (3.21-3.39; 1.98) | 3.21 (3.15-3.25; 2.39) | 3.51 (3.47-3.56; 2.67) | 3.76 (3.73-3.78; 2.93) |
| QEMU TCG `thread=multi`, `nokaslr` | 1.96 (1.93-1.98; 1.26) | 1.71 (1.69-1.73; 1.02) | 1.66 (1.62-1.68; 0.95) | 1.65 (1.62-1.67; 0.96) |
| QEMU TCG `thread=single`, `nokaslr` | 1.92 (1.91-1.94; 1.24) |  |  | 1.86 (1.80-1.91; 1.17) |
| natemu `boot-linux`, cache warm, KASLR | 3.30 (3.24-3.39; 1.97) | 3.48 (3.44-3.54; 2.60) | 3.83 (3.78-3.87; 2.92) | 4.28 (4.20-4.39; 3.34) |
| natemu `boot-linux`, cache off, KASLR | 3.20 (3.19-3.22; 1.95) | 3.24 (3.23-3.25; 2.40) | 3.51 (3.50-3.52; 2.68) | 3.78 (3.71-3.81; 2.94) |
| QEMU TCG `thread=multi`, KASLR | 1.96 (1.95-1.96; 1.25) | 1.70 (1.65-1.73; 0.99) | 1.65 (1.61-1.69; 0.95) | 1.66 (1.66-1.67; 0.95) |
| QEMU TCG `thread=single`, KASLR | 1.96 (1.94-1.98; 1.26) |  |  | 1.85 (1.81-1.88; 1.14) |

| To "Select a language" (initrd): wall s mean (min-max; guest s) | N = 1 | N = 2 | N = 3 | N = 4 |
| --- | --- | --- | --- | --- |
| natemu `boot-linux`, cache warm, `nokaslr` | 13.88 (13.81-13.95; 2.30) |  |  | 10.13 (9.90-10.61; 2.48) |
| natemu `boot-linux`, cache off, `nokaslr` | 16.04 (15.89-16.18; 3.33) |  |  | 13.67 (13.47-13.87; 4.63) |
| QEMU TCG `thread=multi`, `nokaslr` | 6.28 (6.26-6.31; 2.17) | 5.00 (4.97-5.04; 1.74) | 4.92 (4.87-5.02; 1.72) | 4.89 (4.79-5.01; 1.69) |
| QEMU TCG `thread=single`, `nokaslr` | 6.15 (6.11-6.18; 2.16) |  |  | 6.37 (6.32-6.46; 2.18) |
| natemu `boot-linux`, cache warm, KASLR | 16.27 (15.55-16.77; 3.40) |  |  | 14.13 (13.16-14.82; 4.83) |
| natemu `boot-linux`, cache off, KASLR | 16.12 (15.98-16.19; 3.33) |  |  | 13.87 (13.81-13.90; 4.66) |
| QEMU TCG `thread=multi`, KASLR | 6.20 (6.19-6.23; 2.17) | 5.06 (4.99-5.09; 1.76) | 4.86 (4.77-4.92; 1.68) | 4.85 (4.81-4.89; 1.71) |
| QEMU TCG `thread=single`, KASLR | 6.23 (6.23-6.23; 2.19) **1 of 3 did not reach** |  |  | 6.33 (6.11-6.47; 2.19) |

## 6. Non-goals

AES-based RNG inside the VM; guest RAM beyond 3 GiB, more than 4 vCPUs; the correctness pass (differential fuzzing,
real workloads, hardware checks); guest-visible PCID, LA57, TSC-deadline, 1 GiB pages; x87 precision; FEX
code-generation tuning unless guest execution dominates; `ret` through the dispatcher and peer-store stepping unless the
breakdown shows them; the public API surface; anything keyed to Linux; skipping or weakening admission.

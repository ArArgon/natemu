# Performance phase plan (draft)

Status: draft, November 3, 2026, HEAD 335c070 (series47-seg); revised after independent review; chunks 1-12 measured below (chunk 5 uncommitted on d89bc4c, chunk 7 on 0da552a, chunk 6 on ea11225, chunk 8 on 1c4d8b9, chunk 9 on 9ba60d5, chunk 10 on 851cbd2, chunk 11 (phase 2, November 4) on 245700d, chunk 12 on f6e8820). Scope: owner decisions of November 2/3 ([boot-features plan](boot-features-plan.md) §5): faster first-time
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

### Measured (chunk 9: cheaper resident entry, November 3; uncommitted on 9ba60d5, series48-lean)

**Cause first** (design study on 0da552a: about 550 of the about 990 cycles of an `enter` were copies; measured here with a
new perf bucket `enter_copy`, the copies inside `enter`, the rest staying in `enter`). Before (9ba60d5), N = 1 cache off, log
on, all threads: 6.39 M entries, `enter` 0.21 s plus `enter_copy` 0.98 s (0.15 us of copying per entry: an 800-byte
`memset` of the frame, a 1,408-byte `memcpy` of the state into `write_record`'s argument, then the volatile copies of the
state and the frame, 8 bytes a step). Around it, each dispatcher miss loaded the state and `memcpy`d it into `dispatch`, which
`memcpy`d it into `enter`; the linker likewise, and searched the block index twice for the same `svc`. `run_k2.py cost`
(N = 1 M, 5 reps): CLI 0.48 us per iteration, the `svc #DIV` round trip 0.151 us.

**Built** (resident; `crates/runtime/src/record.rs`, `execution.rs`; abi; host one check), in the study's order, each step
measured below:
1. `dispatch`, `enter` and `step` take the state by reference, `write_record` takes `&T`: every call site passes its own
   local (the callees never return), so the copies only went. The fault and step helpers that take a copy keep theirs.
2. `copy_record` copies 32 bytes a step (two `ldp`/`stp` pairs in an `asm!`, which the compiler treats as touching any
   memory, so the copy stays ordered as the volatile one was), then 8: the state record is 44 steps, a frame 25.
3. The entry frame is built in place in `Shared::initial`: `enter` writes the static registers (`SRA`: GPRs, PF, AF), x28,
   SP, v16..v31, FPCR and FPSR at every entry, as it wrote them into a zeroed frame before; nothing writes the rest, which
   the host zeroes and `start` zeroes again. No `memset`, no frame copy.
4. `linker` calls `block_at` once (nothing between the two calls changed the cache).

`tables::select` already returns after one `mrs` when CPL is unchanged (not touched). The perf bucket `enter_copy` (25
buckets, record still at `STATE_OFFSET + 0xe40`) stays. Generated code, the record layouts and the frame's contents are
unchanged. Disassembly (release resident): `enter` no longer calls `memset` or `memcpy` (before: one each, plus the two
volatile copies), `missed`, `dispatch` and `linker` no `memcpy` (before: 1, 2 and 2), `linker` one `block_at` (before 2).
`check_resident_stack.py` `ok`: gate bound 16,368 bytes (before 19,184), with a nested entry 20,512 (23,328), boot 8,672
(12,848).

**Exactness.** The state record and every banked value are written as before; only how many times the bytes are moved
changed. The frame equals the old one when its unwritten rest is zero: `natemu_abi::execution::entry_frame_rest_zero`, which
the host checks at every resident stop (a host error otherwise; unit test with each kind of field). A diagnostic build
(`variant.py diag`, removed) also built the old frame at every entry and halted on any difference: the installer boot at
N = 1 (12.6 s) and N = 4, faults 7,912 at both block sizes, `run_i5a_faults.py` 454 and `delivery` 0 failures, no halt.
Negative controls (`nc/`): `enter` also writing x0 makes every `run_k2.py cost` program a host error ("the resident's entry
frame holds a field it does not write"; 10 of 10, the unchanged build 0); v16 left unwritten makes `run_i5a_faults.py` red
(329 of 454 not as expected).

**Measured** (`natemu-raw/perf9-20261103-01/`: `campaign9.sh`, `one.sh`, `boot.py`, `runs/`, `table9.txt`; installer to
"Select a language", 2 GiB, `mitigations=off nokaslr`, cache off, one VM at a time, a warm-up boot per build, three
interleaved rounds over the five builds; N = 4 one boot each; one log-on boot per build; mean, min-max; no other VM ran):

| Installer, cache off | before (9ba60d5) | 1. by reference | 2. 32-byte copies | 3. frame in place | 4. one `block_at` (**after**) |
| --- | --- | --- | --- | --- | --- |
| N = 1 wall s, log off | 13.09 (13.03-13.20) | 12.19 (12.09-12.25) | 11.10 (11.02-11.18) | 10.89 (10.81-11.00) | **10.82** (10.78-10.87) |
| "Run /init" guest s, N = 1 | 3.02 | 2.87 | 2.72 | 2.68 | 2.66 |
| N = 4 wall s, log off (one boot) | 12.54 | | | | **11.01** |
| Log on, all threads s: `enter` / `enter_copy` | 0.21 / 0.98 | 0.20 / 0.64 | 0.20 / 0.29 | 0.20 / 0.08 | 0.20 / 0.08 |
| Log on: misses / linker entries / guest code / aborts s | 0.82 / 1.68 / 2.03 / 0.73 | 0.56 / 1.42 / 1.99 / 0.62 | 0.45 / 1.18 / 1.84 / 0.47 | 0.44 / 1.17 / 1.81 / 0.47 | 0.43 / 1.09 / 1.81 / 0.46 |
| `run_k2.py cost` us per iteration: CLI / POPF / CPUID / `svc #DIV` | 0.476 / 0.532 / 0.483 / 0.151 | 0.364 / 0.425 / 0.369 / 0.150 | 0.256 / 0.311 / 0.262 / 0.097 | 0.221 / 0.274 / 0.227 / 0.096 | **0.216 / 0.270 / 0.224 / 0.097** |

**Ship threshold met**: -2.27 s (-17%) at N = 1 against spreads of at most 0.17 s, -1.53 s (-12%) at N = 4 (one boot each);
every step faster, nothing slower; all 31 boots (29 here, 2 diagnostic) reached the dialog. An entry now costs 0.044 us in `enter` (0.19 before);
the resident's buckets fall 2.35 s on the log-on boots (`enter` 0.91, linker 0.58, misses 0.38, aborts 0.27, guest code
0.21: `finish`'s frame capture runs before the entry kind is charged), about the wall's gain. A system instruction's round
trip falls from 0.48 to 0.22 us, the division's from 0.151 to 0.097.

**Checks** (`suite/`, `suite9.sh`, on the shipped tree, series48-lean, 2 GiB): `cargo test` 160 + 1 ignored, also with `FEXC`
set; `check_resident_stack.py` `ok` (16,368 / 20,512 / 8,672 bytes: 2,816, 2,816 and 4,176 less than chunk 8's); fixtures 64
and seed 44 at both block sizes; faults 7,912 at both; `run_slice3_vm.py` `delivery`, `events`, `system`, `contain` (423
controls) and `smp` 0 failures; `run_k1.py` 248 runs, no failure, 14 and 14 requests at K and 2K rounds; `run_k2.py`;
`run_s4.py` (QEMU's two known aborts on `s4-d-stack` recorded as departures, as before); `run_x1.py` 6/6 and 196/196;
`run_x3.py` 9/9, 4/4, 24/24, 6/6, 10/10, 54/54; `run_i5a_faults.py` 454; `run_i5c.py` 255 at both block sizes;
`run_i6b.py` corpus 112 and cases 129 at both; `run_boot_smoke.py` 4/4; `run_r1.py r1a` 7/7 and `r1b` 12/12 (negative
controls flagged); all 0 failures. The first pass ran out of disk at `system` (earlier suites' per-case outputs had
filled it); after space was freed `suite9.sh` reran from `system` on (`FROM=system`), deleting each case's output as it
ended (`results-pass1.txt` keeps the first pass). Per-case outputs and the
per-request logs deleted after analysis.

### Measured (chunk 10: reuse inside the resident, November 3; uncommitted on 851cbd2, series48-lean)

**Built** (resident, abi, host; fexc unchanged; the design study of chunk 6's question 2, with chunk 8's records). A block miss
whose translation another block of this vCPU already holds at another RIP is placed by the resident itself, with no host exit:
- *Template (host).* `translation` sets `State::relocatable` when fexc's answer at the RIP is its answer for the same bytes at
  every RIP of that half and page offset (chunk 3's proof) and depends only on bytes of the block's own pages: not a step, no
  limit (breakpoint, hook, stop RIP) cut the fetched bytes, no halving retry, not `unsupported_form`, fexc's extent short of the
  bytes' end and on the block's pages (chunk 6's rule; now kept per compile, not only with the log). `place` keeps the
  admitted object's guest-RIP literals and moves after the block's sites, packed u32 (offset, move bit, register; at most
  255, count in `Block::flags`), and marks the block `RELOCATABLE`.
- *Hint table* (`cache::HINTS`, 1 MiB of the lookup area per vCPU, which grows from 14 to 16 MiB): u32 block number + 1 by a
  hash of (frame, page offset and half, context), written at every `RELOCATABLE` filing. A hint is only a candidate:
  `cache::template` checks the record (live, `RELOCATABLE`, not a step or system record, same frame, offset, half, context),
  so flushes never clear it.
- *Miss path* (`execution::reused`, before the request): RIP's page is walked and marked as for a request. With a template, and
  no instruction breakpoint, no stop RIP and no host fence (`Control::reuse_fences`: the test driver's hook RIPs and handler
  range) in the bytes the host would fetch, a crossing template's second page is walked and marked too; pending invalidations
  are taken; the template must still be live and, when it crosses, its second frame the one walked. `cache::copy` copies its
  code above the fill point and rewrites it with `natemu_abi::execution::relocate_code`; it is filed as an ordinary block
  (chained on its frames, linear pages, L2, hint) and kept by `kept` as a host placement is. Anything refused goes to the host
  with the walk already made (the second page's walk undone), counted (`Control::reuse_refused`, by kind).
- *The re-check* (`relocate_code`, abi, about 60 lines; the host's relocator now uses the same encoder and move rule): the RIP's
  half and page offset are the template's and the RIP and block end canonical; the relocations are in offset order, none
  starting inside the one before (`place` keeps them sorted; admission refuses no duplicated or overlapping record, and a move
  rewritten twice would hold + 2d); link points are restored to their admitted words;
  each literal slot gets + d and must stay canonical; each move is re-encoded by FEX's `LoadConstant` rules
  (`relocate_move`, shared with `relocation::object`): a 64-bit form before and after, the new value canonical, as many words,
  and the new words with the next word decoding to the new value in as many words; each link point gets its move's new first
  word and target + d and must be a link point after (`link_point`, shared with the reply check). Every other admission rule
  reads offsets, bytes or values that move with d (design study, section 2).
- *Check mode* (`Control::reuse` = `REUSE_CHECK`, set by `boot-linux --check-reuse` and the program driver's `--code-shape` or
  `--check-reuse`, so by every suite runner): each copy is also asked of the host at its RIP (through the walks it was made
  from) and must equal its staged answer: code (the computed exit's word `b` to the dispatcher from the copy), sites, link
  points, exits, pages, the relocations it carries and the host's template bit;
  otherwise the vCPU stops `reuse_mismatch` (64).
- Counters: the K1 statistics' `reuse` (copies, refusals by kind, checks), the boot report's per-vCPU `requests` and `reuse`;
  perf bucket `reuse` (26 buckets; the second page's walk stays in `fetch`).

**Tests.** Unit (`relocation.rs`): the re-check turns the block admitted at one fixture RIP into the one admission gives fexc's
object at the other, both ways, its links linked first; each refusal (another offset or half, a non-canonical RIP or block end,
another word count, a 32-bit form, a move outside the code or at no move, a literal outside the code or made non-canonical, a
move or literal listed twice, a literal starting inside another, the relocations out of order, a link point no move rewrote
or with another target; the duplicated move is red without the order check) and the move rule's. K1 (`run_k1.py`, natemu only, 1-4 vCPUs, both block
sizes, values asserted and least copies and refusals): `reuse-alias` (a frame at RV1 in table A and RV2 in B, above 4 GiB:
the second runs copies, with its own `lea [rel]` and return-address values), `reuse-write` (the frame written after a copy: the
copy dies with its template, a third alias is not copied from the dead ones), `reuse-remap` (B's alias remapped to another
frame with INVLPG: its own bytes), `reuse-cross` (a block crossing into a second page: copied where both frames are the
template's, not where the second differs). Each red under its mutation (`natemu-raw/perf10-20261103-01/mut/`: `build.py`, `run.py` and their logs; scratch residents), with the suites' check on
and off: moves and link targets not rewritten (`reuse-alias`: `reuse_mismatch`; off: RV1's values, or a fault), a copy left off
its frames' chains and a dead template copied (`reuse-write` r10/r14 = 1), the frame left out of the hint and the check
(`reuse-remap` r10 = 1), the second frame not compared (`reuse-cross` r14 = 0x100). Literal slots not rewritten is red only
under the check (`reuse_mismatch`): no guest-visible value reads them in these programs (FEX's tail holds the block RIP).

**Check runs** (`natemu-raw/perf10-20261103-01/check/`, `boot-linux --check-reuse`, installer, cache off): N = 1 849,848
copies, each also answered by the host and equal; N = 4 (vCPU 0) 211,439 copies checked; 0 mismatches, both reached the
dialog. Every suite runner below ran with the check on (the suites' `--code-shape`): no `reuse_mismatch`.

**Measured** (`natemu-raw/perf10-20261103-01/`: `campaign10.sh`, `one.sh`, `boot.py`, `runs/`; installer to "Select a
language", 2 GiB, `mitigations=off nokaslr`, cache off, one VM at a time, a warm-up boot per build, three interleaved rounds at
N = 1, one boot each at N = 4 and one log-on boot (perf resident) per build at N = 1; mean, min-max):

| Installer, cache off | before (851cbd2) | after |
| --- | --- | --- |
| N = 1 wall s, log off | 10.71 (10.65-10.78) | **8.57** (8.55-8.61) |
| N = 4 wall s, log off (one boot) | 10.98 | **9.37** |
| "Run /init" guest s, N = 1 / N = 4 | 2.64 / 4.14 | 2.42 / 3.98 |
| Host block requests, N = 1 (all four vCPUs at N = 4) | 1,155.6 k | 302.0 k (446.2 k) |
| Copies in the resident / refused (move word count; own: fence, second frame, dead), N = 1 | | 850.1-860.1 k / 0.84-1.0 k; 4-90 |
| Copies at N = 4, all vCPUs / refused | | 890.4 k / 636; 5 |
| Log on, vCPU 0 s: host exits (count) / `hv_vcpu_run` / host translate (of it admission, relocation) | 5.78 (1.248 M) / 6.84 / 4.00 (0.93, 0.24) | 3.31 (0.386 M) / 5.93 / 2.69 (0.29, 0.02) |
| Log on, vCPU 0 s: resident `reuse` (count) / `place` | - / 0.174 | 0.268 (850.9 k) / 0.060 |
| Max RSS MB, N = 1 / N = 4 | 612 / 1,201 | 610 / 1,187 |

**Ship threshold met**: -2.14 s (-20%) at N = 1 against spreads of at most 0.12 s, -1.61 s (-15%) at N = 4 (one boot each);
nothing slower; every boot reached the dialog. Host requests avoided: 853.6 k at N = 1 (74%; chunk 6 bounded 870 k), 890 k at
N = 4. Fallbacks: about 0.1% of the templates found, nearly all a move needing another word count (as the host relocator's
refusals). Resident time added: 0.27 s of `reuse` for 851 k copies (0.31 us each, re-check and placement included) against
0.11 s less host placement; the host gives back 2.5 s of exit and request time (fexc's 2.0 s unchanged: the compiles are
the same 126 k). Memory: 4 bytes per guest-RIP relocation of a template in the arena (packed only when reuse is on) and the
hint table's 1 MiB per vCPU (the lookup area 14 to 16 MiB of backing; RSS unchanged).

**Independent review** (read-only, of the diff): no blocking defect. Applied: the fence test no longer wraps at the top of
the address space; the check compares the code's first word when there is no computed exit; a write posted while the check's
host request ran (the host may have read the new bytes) now kills the copy and steps instead of halting `reuse_mismatch`;
relocations are packed only when reuse is on; the reply check bounds the staged relocation table as it does the sites.
After the coordinator's containment review (nothing blocking): the order and overlap rule above and the check's dispatch word,
relocations and template bit; rerun (`followup/`): `cargo test` 162 + 1 ignored with `FEXC` set, the four reuse programs (32
runs) green, a `--check-reuse` installer boot at N = 1 851,238 copies checked, 0 mismatches, 968 + 7 refused (as before).
Noted, not changed: a refused crossing copy leaves its second page marked and walked (A bits; conservative); and, before this
chunk, the host ignored a limit at the first byte of the next page (`fetched`'s `span`), which in-VM reuse cannot reach (it
refuses at any armed breakpoint and fence). Fixed after 87c40c1: a limit at a page's end now cuts its bytes there, so the next
page is neither fetched nor asked for (before, a block starting on the previous page ran past a breakpoint, hook or stop RIP at
the next page's first byte); the resident's fence test is `natemu_abi::execution::reuse_fenced`, unit-tested at the page
boundaries and the top of the address space. Rerun (`span/`): `cargo test` 163 + 1 ignored with `FEXC` set, fixtures 64,
faults 7,912, `run_k1.py` 280 runs, `run_s4.py`, `run_x1.py` 6/6 and 196/196, `run_boot_smoke.py` 4/4.

The measurements are of the build before these changes (none is on the boot's path but the check's); the final build, in two
interleaved rounds with the measured one later (the machine then a little slower): N = 1 8.78 and 8.87 s against 8.76 and 8.81 s.
On the final build, with the check (`check/final-*`): N = 1 848,442 copies, N = 4 871,600 over the four vCPUs, all equal to the
host's answers; without it 840.7 k and 866.1 k copies, 301.6 k and 449.2 k host block requests.

**Checks** (`suite/` on the build before the review's changes and `final/` on the shipped tree, `suite10.sh`, the same
commands, series48-lean, 2 GiB, one VM runner at a time, each case's output deleted as it ended; both passes green):
`cargo test` 162 + 1 ignored, also with `FEXC` set (2 new); `check_resident_stack.py` `ok` (16,656 / 20,832 / 8,912 bytes: +288,
+320, +240 on chunk 9's); fixtures 64 and seed 44 at both block sizes; faults 7,912 at both; `run_slice3_vm.py` `contain` (423
controls), `smp`, `sharedpt`, `coherence`, `delivery` 0 failures; `run_k1.py` 280 runs (the four new programs at 1-4 vCPUs and
both block sizes), no failure, 14 and 14 requests at K and 2K rounds; `run_k2.py`, `run_s4.py` all ok; `run_x1.py` 6/6 and
196/196; `run_boot_smoke.py` 4/4; `run_r1.py r1a` 7/7 and `r1b` 12/12 (negative controls flagged); every installer boot
reached the dialog. Not rerun: the rest of the standing suite (the change is in the resident's miss path and placement, which
these exercise, every one with the reuse check on).

### Batch 1 result (November 3, HEAD 4ed8e3a, series48-lean)

Chunks 1-10 against the starting point and QEMU, rerun at the batch's end: the `natemu` stub (built alone, signed) and QEMU 11.1.2
TCG in the like-for-like harness of §5 (handoff §4; `natemu-raw/batch1-final-20261103-01/timing/`: 149 boots, every one reached
its end point, none contended), `nokaslr`, 2 GiB, wall s mean of 3, one VM at a time. Start = the handoff's stub rows at 335c070
(one boot per cell, R2's harness; the plan's §1 table differs by up to 0.1 s). The standing suite is green at both RAM sizes
(handoff §5, "Full suite at the end of performance batch 1"; the 7 known i8b departures only; `--check-reuse` installer boots
0 mismatches).

| Wall s | start (335c070) | end (4ed8e3a) | QEMU multi | QEMU single | end / QEMU multi |
| --- | --- | --- | --- | --- | --- |
| Installer N = 1, cache off | 67.8 | **8.85** | 6.10 | 6.12 | 1.45x |
| Installer N = 4, cache off | 53.1 | **9.39** | 4.81 | 6.26 | 1.95x |
| Installer N = 1, cache warm | 68.2 | **6.89** | 6.10 | 6.12 | 1.13x |
| Installer N = 4, cache warm | 50.2 | **5.80** | 4.81 | 6.26 | 1.21x |
| Kernel only N = 1, cache off | 4.15 | **2.69** | 1.93 | 1.93 | 1.39x |
| Kernel only N = 4, cache off | 5.70 | **3.31** | 1.66 | 1.85 | 1.99x |
| Kernel only N = 1, cache warm | 2.40 | **1.69** | 1.93 | 1.93 | 0.88x |
| Kernel only N = 4, cache warm | 2.36 | **1.57** | 1.66 | 1.85 | 0.95x |

Installer, cache off, per step (each chunk's own before and after in its rounds, N = 1 / N = 4 wall s): chunk 2 (fexc's report off the
serve path) 67.9 to 49.3 / 53.6 to 37.6; chunk 4 (reuse across load addresses, in the host) 49.2 to 27.7 / 37.6 to 22.8; chunk 5
(one L1 table per code context) 27.7 to 17.4 / 22.6 to 15.6; chunk 7 (host per-request cleanups) 17.8 to 15.9 / 15.7 to 13.6; chunk 8
(links kept across TLB events) 15.6 to 13.6 / 13.4 to 12.4; chunk 9 (cheaper resident entry) 13.1 to 10.8 / 12.5 to 11.0; chunk 10 (reuse
inside the resident) 10.7 to 8.6 / 11.0 to 9.4. Chunks 1 (instrumentation), 3 (relocation proof) and 6 (whole-cache flushes) changed no
wall time. Chunks 2, 4 and 5 account for 50 of the 59 s at N = 1; the later four, each about 2 s, bring it from 17.4 to 8.6 s. At N = 4
the installer is no faster than at N = 1 with the cache off (9.39 against 8.85 s): the next phase's focus is multi-core.

### Measured (chunk 11: link records at capacity, November 4; uncommitted on 245700d, series49-sha)

**Cause** (phase-2 baseline, [perf-phase2-plan.md](perf-phase2-plan.md) "Baseline (November 4)"; reproduced here,
`natemu-raw/linkfix-20261104-01/repro/`: `build` at N = 1, perf resident, 60 s cap: `patch` 52.9 of 57.6 s of thread CPU, 252,731
fills). No record leaked: every dead block's links were pruned, and a fill's strict prune found nothing to free. The 64 Ki
records filled with links that stay valid: into `GLOBAL` kernel blocks (current across every CR3 load until one of the run's
60 global flushes) and local links into current blocks. Chunk 8's rule at a fill (undo the local links into blocks not current,
then link if room) then freed about one record per pass (234 k records over 449 k fills in the baseline's 100 s), so almost every
later linker entry scanned all 64 Ki records, linked nothing, and its exit came back to the linker the next time.

**Built** (resident `cache::link`, abi). A link can always be dropped: its point then enters the linker again. At a fill, the
strict prune runs first (chunk 8); if that leaves less than a quarter of the area free, every link is undone (`unlink_all`, as at a
flush, the blocks kept). Each fill then costs at most two passes over the area and is followed by at least a quarter of the area's
links before the next one: amortised O(1) per link, and no linker entry scans the area unless it fills. `Link::Full` is gone (the
State field `full_links` stays, always 0). Capacity: `MAX_LINKS` = `MAX_BLOCKS` (256 Ki records, 4 MiB; 64 Ki before), so the
metadata arena (64 MiB, about 238 Ki of `build`'s blocks) fills before the records do: at 64 Ki with the new rule, `build` N = 1
filled 73 times (3.70 M links dropped, 4.89 M made, 22.85 s); at 256 Ki never (2.12 M made, 21.01 s; one perf boot each,
`diag/`). The lookup area grows by 2-3 MiB per vCPU (2 MiB-rounded); every layout assertion holds. Kept: K1 exactness and
chunk 8's invariants (links are only removed; a restored point goes back through the linker's currency-checked lookup; the
records stay contiguous in `[0, links)`; `INBOUND` and `LOCAL_INBOUND` may stay set, which only costs a pass).

**Test** (`tools/run_k1.py` `link-full`, natemu only, 1-4 vCPUs, both block sizes): per CPU a chain of 70 pages above 4 GiB of
`jz $+2` (286,720 link points against 262,144 records), three passes per phase: as placed; after CPU (i - 1) % N rewrote a
page late in the chain (its links live then); after a page remapped with a CR3 reload; back with INVLPG. Every value exact (r8 3,
r9 6, r10 12, r12 6), the area filled in every pass with no flush, and fills at most 1 + 4 links / `MAX_LINKS` (13 for 3.44 M
links); about 25 s per run. Negative controls (scratch residents, `k1-mut/`): chunk 8's rule kept at 256 Ki: 135 k fills (bound
9), 142 s against 48 s for both block sizes; a fill that drops the records without restoring the words: the host's link check
fails (24,583 records for 286,725 linked points). A death that prunes only the first two segments stays green: every local
link's source dies with its target (chunk 8's note).

**Measured** (`natemu-raw/linkfix-20261104-01/`, `campaign.py`, `runs/`, `inst/`, `perf/`; the `natemu` stub, cache off,
`mitigations=off nokaslr`, 2 GiB, one VM at a time, none contended, every run verified; median (min-max) of three, START to END
seconds; before = the baseline of the same day):

| Benchmark | N | before | after | QEMU multi |
| --- | --- | --- | --- | --- |
| `build` | 1 | did not finish (>2400) | **21.25** (20.89-21.25) | 28.3 |
| | 2 | 59.3 (48-121) | **15.84** (15.53-15.88) | 15.3 |
| | 3 | 73.9 (41-88) | **13.67** (13.54-14.55) | 13.5 |
| | 4 | 51.1 (35-119) | **13.93** (12.61-14.43) | 12.5 |
| `hackbench` | 1 | 53.81 (53.53-56.34) | 52.73 (52.33-53.19) | 16.4 |
| | 4 | 15.20 (15.17-15.25) | 15.21 (14.93-15.32) | 5.08 |
| `locks` | 1 | 16.69 (16.58-17.24) | 16.30 (16.28-16.35) | 11.0 |
| | 4 | 5.81 (5.75-5.82) | 6.07 (5.99-6.09) | 27.5 |
| `locks` (interleaved) | 4 | 6.10 (5.69-6.14) | 5.91 (5.79-6.36) | |
| Installer N = 1 (interleaved) | 1 | 8.80 (8.74-8.83) | 8.83 (8.75-9.01) | 6.10 |

`build` perf boots: N = 1 `patch` under 0.3 s (not in the top eight buckets), no fill, 2.15 M links made; N = 4 no fill, `link`
0.59-0.68 s per vCPU (14.5-17.8 s of `patch` before). `hackbench` and `locks` spend their `link` bucket in linker entries whose
target is a system instruction (hackbench N = 1: 98.6 M entries, 114.6 M system instructions, 0.2 M links, no fill): nothing to
link there; that is phase 2's item 7 (system-instruction fast paths), not the records.

**Independent review** (read-only, of the diff): no blocking defect; it traced `unlink_all` inside `link` (contiguity, flags,
armed stops, REP self-links, the host's check), the recount, the amortisation bound and the layout. Applied: the rewritten page
moved late in the chain (page 20 had no live links when written), CPU (i - 1) % N in the comments, a doc wording. Noted: the larger
area could lengthen CR3 and strict prunes (measured: no slowdown above; `tlb` 0.35 s over the whole N = 1 build).

**Checks** (`suite/`, `suite11.sh`, series49-sha, one VM runner at a time, each case's output deleted as it ended): `cargo test` 163 + 1
ignored, also with `FEXC` set; `check_resident_stack.py` `ok` (16,656 / 20,832 / 8,912 bytes, unchanged); fixtures 64 and seed 44
at both block sizes; faults 7,912 at both; `run_slice3_vm.py` `contain`, `smp`, `sharedpt`, `coherence` 0 failures; `run_k1.py`
288 runs (`link-full` at 1-4 vCPUs and both block sizes: 13 fills each), no failure, 14 and 14 requests at K and 2K rounds;
`run_k2.py` all ok; `run_boot_smoke.py` 4/4; `run_r1.py r1a` and `r1b` ok (negative controls flagged). Not rerun: `run_s4.py`,
`run_x1.py`, `delivery` (the change is in the link records, which these exercise only as the cases above do).

### Measured (chunk 12: block exits into system instructions, November 4; uncommitted on f6e8820, series49-sha)

**Cause** (`natemu-raw/sysfast-20261104-01/measure/`; the perf resident with new per-kind counters, `hackbench` N = 1, 50.9 s):
114.5 M system instructions, 98.5 M linker entries and 16.0 M dispatcher misses into them, 115.8 M block entries. By kind: SWAPGS
32.2 M, CLI 16.7 M, STI 16.5 M, RDTSC 16.1 M, SYSCALL 16.0 M, SYSRET 16.0 M (Linux's syscall path: `entry_SYSCALL_64` is
`endbr64; swapgs`, the exit `swapgs; 7 NOPs; sysretq`, RDTSC is the kernel-stack offset), everything else 0.9 M; the installer
N = 1: 2.5 M (CLI 24%, STI 21%, MOV from Sreg 13%, SWAPGS 11%, IRET 8%, RDTSC 6%). Cost per system instruction, from
temporary sub-buckets (`prof`, `prof2` runs): transaction 75 ns (SWAPGS 92, SYSCALL 126, SYSRET 97, CLI 34: two or three
copies of the 800-byte bank each), the boundary checks of `find` 35 ns per pass (two passes; the pending word's SeqCst
swap and the LAPIC poll's `isb`), `block_at`'s binary search 29 ns, the state load and bank 22 ns, `enter` 50 ns, the L1
table switch at each CPL change 88 ns (32 M per run: the dispatcher's `adr` rewritten with cache maintenance), plus the
exception gate and `eret` in `guest`. `run_k2.py cost` (N = 1 M): CLI 0.211 us, SWAPGS 0.272, RDTSC 0.233, `svc #DIV`
0.096 per iteration.

**Built** (resident, host, abi; each measured on hackbench N = 1 with the perf resident, 50.9 s before):
1. Transactions without bank copies: SWAPGS, SYSCALL, SYSRET, SYSENTER/SYSEXIT, RD/WR FS/GS BASE, RDRAND, WBINVD and
   CLFLUSH make every check before their first write, so a fault leaves the bank as it was: their rollback copy goes (only
   LAR..VERW keep it). RDTSC/RDTSCP (now an early arm) and CLI/STI/POPF never restored a copy (each checks before it
   writes): they only lose the copy `transaction` made before its arms, which they never used. 2. `drain` loads the
   pending word before swapping it (swap only when non-zero); `lapic::poll` reads CNTVCT without `isb` (an early read can
   miss an expiry just due; none is lost: `cntv`, ending in an `isb`, leaves the virtual timer armed at the deadline, and
   its IRQ's `timer_irq` reads the count with the barrier, so a boundary after that IRQ sees it). 3. `block_at` tries a per-vCPU hint by code word (`Control::pc_hints`, 2 Ki), used only when the hinted
   block's code still holds the word (ranges never overlap). Together 50.9 -> 36.8 s: transaction 27.7 ns average, the
   boundary checks 14 ns per pass, `block_at` 2.2 ns. 4. **NOP runs** (`system::NOP_RUN`): a translate request whose block
   fexc cut before a system instruction and which holds only no-operation instructions (host `system::nops`: `90`, `66 90`,
   `0f 1f /0` with 66, CS/DS/ES/SS and REX prefixes, ENDBR64/32; LOCK, F2/F3, 0x67, FS/GS, PAUSE, `41 90` refused; the bytes and
   count are fexc's) is answered as one system decode of those bytes, kept as a K2 record and committed as one transaction,
   so `find` runs the next system instruction in the same resident entry: `endbr64; swapgs` and `swapgs; nops; sysretq` take
   one entry each instead of two (115.8 M -> 83.9 M block entries; 36.8 -> 32.4 s). It is never a step (`kept_system` refuses
   it: under TF, an interrupt shadow or a held data trap the host steps the first instruction alone). 5. **A dispatcher per
   code context**: the head holds 127 copies of the dispatcher ("slots"); a context gets one when it first takes an L1 table
   (until the next flush), its slot's `adr` names that table, and its blocks' computed exits branch to it (`dispatcher_for`
   at placement, copies too); a table taken by another context makes the old one's slot miss (`b` to the miss) and binds the
   new one's; a global TLB flush and a reset unbind the tables they empty. A context change then writes no code (it wrote the
   dispatcher's `adr`, 88 ns): 14 slot writes for 32.0 M switches (32.4 -> 29.2 s). No context without a slot is ever
   entered through L1: its blocks exit to the miss. Rejected after measuring: linking a block's exit straight into a kept
   system record (a stub entering the transaction): with the hint the linker's own cost before `find` is about 20 ns of a
   system instruction's 200 ns and the first lookup 7 ns, not worth a new entry kind; translating CLI/STI/SWAPGS inline needs
   fexc changes and emulator state in the guest-state page.

Per x86 rule (SDM Vol.2 instruction pages, Vol.3 6.8.3, 7.8.3, 18.3.1), and where it is kept: SWAPGS #UD with LOCK, #GP(0) at
CPL > 0; SYSCALL #UD without EFER.SCE, RCX/R11, RFLAGS & ~FMASK, CS/SS from STAR, CPL 0, a TF at its start traps after it;
SYSRET #UD without SCE, #GP(0) at CPL > 0 or a non-canonical RCX (before any write), RFLAGS from R11; CLI/STI #GP(0) at
CPL > IOPL, STI's shadow only when IF was 0; RDTSC/RDTSCP #GP(0) with CR4.TSD at CPL > 0, RDTSCP's ECX: all unchanged, every
check still before the first write (the reason the syscall group's rollback copies could go; RDTSC/RDTSCP and CLI/STI never
restored one). Interrupt windows: `find` takes events at every boundary between chained instructions, NOP runs included; a shadow with an event pending steps one instruction (never a
NOP run). TF: every traced instruction is stepped alone. Instruction breakpoints: blocks are cut at armed ones and arming
flushes, so no NOP run spans one; RF ends after the run as after a block. Faults: NOPs take none (their memory operand is
not accessed). Cross-modifying code: a NOP run is a K2 record of its bytes, invalidated as any.

**Tests.** `run_s4.py` `s4-nops` (a 20-byte run before CLI made plain, then run under TF: each NOP-class instruction, CLI and RET
trap after themselves; an instruction breakpoint inside the run faults there), judged by the SDM and equal to QEMU at both
block sizes; `run_slice3_vm.py events` `ev-sti-nops` (STI with an interrupt pending before a kept run: taken after the first
NOP) at 1, 2, 4 vCPUs, judged by `delivery_model` and QEMU; `run_k2.py` coherence `G-nop-run-own-write` (the run's own write
makes STC of its second NOP: the next visit sets CF); host unit test `nop_runs`. Negative controls (scratch residents,
`t/nc*`): `kept_system` not refusing NOP runs: `s4-nops` red (11 entries) and `ev-sti-nops` red (14); a slot not renamed when
its context takes a table again (and no miss at hand-over): `k1 contexts` red (60 failures). Making the old slot miss at a
hand-over is defence in depth: a context's blocks run only while it is current, and taking a table always binds its slot
(that mutant alone stayed green). Independent review (read-only, of the diff): no blocking defect; applied its two
performance findings (a global flush left tables pointing at slots, so a later hand-over could make a live context miss
until its table changed hands; the head filled after the start's reset, leaving the current context without a slot) and a
comment.

**Measured** (`natemu-raw/sysfast-20261104-01/`, `campaign.py`, `runs/`, `inst/`, `perf/`, `ab/`; the `natemu` stub, cache off,
`mitigations=off nokaslr`, 2 GiB, one VM at a time, base and fix interleaved, every run verified, runs another agent's VM
overlapped discarded (`clean.py` retries them); median (min-max) of three, START to END seconds):

| Benchmark | N | before (f6e8820) | after | QEMU multi |
| --- | --- | --- | --- | --- |
| `hackbench` | 1 | 51.17 (50.01-51.55) | **30.10** (29.61-30.10) | 16.4 |
| | 4 | 15.36 (15.15-15.59) | **9.57** (9.45-9.63) | 5.08 |
| `locks` | 4 | 6.42 (6.40-6.44) | **5.35** (5.18-5.35) | 27.5 |
| `build` | 1 | 20.89 (20.76-21.12, five) | **20.39** (20.19-24.07, seven: five in 20.19-20.51, then 21.60 and 24.07) | 28.3 |
| Installer, cache off | 1 | 8.97 (8.70-9.06) | **8.46** (8.36-8.49) | 6.10 |

"after" is the campaign's build; the build with the review's fixes (`fix2`) gave `hackbench` N = 1 30.35-30.50, `build` N = 1
20.23-24.07 (the `build` row holds its five), `locks` N = 4 5.17 against the campaign build's 5.21-5.24 and the base's 6.02-7.33
in one interleaved set (`ab/`). Perf boot after (fix2, hackbench N = 1): 147.1 M system instructions of which 32.5 M NOP runs and
64.6 M chained (no block between), 66.5 M linker entries, 83.9 M block entries; buckets `guest` 11.9, `link` 6.9, `enter` 5.0,
`system` 3.8, `lookup` 1.8, `miss` 1.4 s; `l1` gone (14 slot writes). N = 4 per vCPU: `link` 1.9-2.0 s (4.1-4.3 before), `system`
1.0-1.1 (2.5). `hackbench` is still 1.8-1.9x QEMU: each syscall is five resident round trips of about 0.2 us.

**Checks** (`suite/`, `suite12.sh`, series49-sha, the final tree, one VM runner at a time, each case's output deleted as it ended):
`cargo test` 164 + 1 ignored, also with `FEXC` set (one new); `check_resident_stack.py` `ok` (16,560 / 20,736 / 8,800 bytes, -96 /
-96 / -112); fixtures 64 and seed 44 at both block sizes; faults 7,912 at both; `run_slice3_vm.py` `system`, `delivery`,
`events` (`ev-sti-nops` included), `contain`, `smp` 0 failures; `run_k1.py` 288 runs, no failure; `run_k2.py` all ok (`G`
included); `run_s4.py` all ok (`s4-nops` included; QEMU's known departures as before); `run_x1.py` 6/6 and 196/196; `run_x3.py`
all ok; `run_l4a.py` all ok; `run_boot_smoke.py` 4/4; `run_r1.py r1a` 7/7 and `r1b` ok (negative controls flagged). Not rerun:
`sharedpt`, `coherence`, `run_m1.py`, `run_x4.py` (the change is in system transactions, the boundary checks, block placement and
the dispatcher, which the runs above exercise).

**Follow-up tests** (after an independent review of 330e014, no blocking defect; `natemu-raw/sysfast-followup-20261104-01/`,
series52-adox66, HEAD 18ee440; each with a scratch mutant, `mutant-*.diff`, that turned it red, then reverted):
`run_k2.py rollback` (SYSRET with a non-canonical RCX, WRGSBASE with a non-canonical value: #GP(0) with the frame's RIP, CS
and RFLAGS, RCX and the GS base as before; red when SYSRET writes RFLAGS, or WRGSBASE the base, before the check);
`run_k1.py hint-reset` (a flush between rounds renumbers blocks at the same code addresses; 1-4 vCPUs green; red with the
hint unvalidated, everything then naming block 0, and with any recorded hint trusted, from the round after a renumbering,
as do `link-full` and, at block size 1, `contexts`); `run_slice3_vm.py events` `ev-movss-db-nops` (MOV SS whose
load meets a data breakpoint, before a kept run: #DB after the first NOP, delivery_model; QEMU takes it before the NOP, listed
in `QEMU_EVENTS_DIFFER`; red, with `ev-sti-nops`, when `kept_system` returns NOP runs); `run_k2.py nopruns` at 1-4 vCPUs:
`cross` (a run crossing into a page made not present: #PF at the crossing NOP, CR2 the page, error 0x10; then remapped: the new
bytes; red when a run's record omits its second page) and `peer` (vCPU (j + 1) % N rewrites j's run, then a flag, CPUID: the
new bytes; red when runs are on no page's chain, which also breaks own writes). NOP runs exist only at the default block size
(block size 1 green under the mutants). `--check-reuse --code-shape` installer boots, cache off: N = 1 852,497 copies checked,
N = 4 (vCPU 0) 205,825, no halt, both reached the dialog.

### Measured (chunk 13: one answer cache for every vCPU, November 4; uncommitted on 18ee440, series49-sha)

**Built** (host only: `execution.rs`, `linux.rs`; relocation, admission, the disk cache and the resident unchanged; the design
study of the phase-2 plan's item 2). `Answers`: fexc's answers as kept before (raw reply, the RIP compiled at, the bytes to the
extent, the gating values read), in 16 shards, each a `Mutex` around today's two generations at a sixteenth of the bounds
(16,384 answers or 8 MiB per generation and shard; at most 256 MiB in all). The shard is chosen by a separately keyed
`Universal` hash of the block limit and the RIP's half and page offset, so every lead length of one request is in one shard and
a lookup takes one lock; a lock taken at the first `try_lock` is not timed. Entries are `Arc<Entry>`: a lookup returns a
reference, and relocation and admission run outside the lock, so a peer's generation flip between lookup and use only drops the
map's reference. `boot-linux` (`Prepared::run`) makes one map per run; each vCPU's `Compiler::share` takes it when its
`supported` features equal the map's (the key leaves them out, fexc's request carries them), else keeps its own and says so (never
seen). Each vCPU keeps its own `fexc --serve` and relocates and admits every answer at its own RIP with its own bytes; the
`--check-reuse` check, the disk-cache order and the tamper hook (on the admitted copy) are unchanged. The program driver keeps
one map per vCPU (its hooks count per-vCPU compiles). Counters (performance log, vCPU `state`): `hits_by_source` (memory hits by
the vCPU that kept the answer), `duplicates` (a compile after a lookup that found nothing whose insert finds a matching answer
another vCPU kept meanwhile), `lock_wait` (ns, waits); the run line `answers {"lock_waits": [[ns, waits] per shard],
"kept_bytes", "kept_answers"}` (the map's totals; the vCPU line no longer has them). A generation a flip drops is freed after
the shard is unlocked.

**Tests** (`execution.rs`): colliding keys (one key, other later bytes or consulted gating: told apart; another half, offset or
limit: no match; the requested RIP's answer first; a duplicate's insert names the holder); an answer evicted by two flips of its
shard between lookup and use stays whole for the holder and is gone from the map; four threads inserting and looking up across
generation flips (every answer found is one kept for the bytes asked, cross-thread hits found); with `FEXC`, two compilers on two
threads sharing one map (the second's request at another RIP is the first's answer, relocated, checked against its own compile,
0 compiles, counted by source). The existing reuse tests edit kept entries through `Arc::get_mut`.

**Check run** (`check/check-n4-log`, `natemu-host boot-linux --check-reuse`, installer N = 4, cache off, log on): 966,430 memory
hits used at another RIP, each also compiled there and equal (629 refused, 0 fallbacks), 861,825 resident copies checked, 0
mismatches; 132.2 k compiles; the dialog reached.

**Measured** (`natemu-raw/shared-20261104-01/`: `campaign.py`, `run1.sh`, `bench.py`, `boot.py` (now reaping with `wait4`:
process CPU and max RSS, the fexc children not included), `summ.py`, `table.txt`, `inst/`, `runs/`, `log/`; the `natemu` stub,
cache off, `mitigations=off nokaslr`, 2 GiB, one VM at a time, base (18ee440) and fix interleaved, every run uncontended and
reached or verified; median (min-max) of three; installer to "Select a language", benchmarks START to END; both stubs built
with the test driver, so with the host's `testing` hooks):

| Run | N | wall s before | wall s after | process CPU s before / after | max RSS MB before / after |
| --- | --- | --- | --- | --- | --- |
| Installer | 1 | 8.65 (8.65-8.72) | 8.70 (8.54-8.82) | 8.70 / 8.72 | 611 / 556 |
| Installer | 4 | 9.17 (9.06-9.19) | **6.63** (6.57-6.69) | 15.07 / 10.40 | 1,206 / 925 |
| `build` | 4 | 12.95 (12.14-13.21) | **9.26** (9.19-9.34) | 43.94 / 31.85 | 2,235 / 1,545 |
| `hackbench` | 4 | 9.60 (9.54-9.61) | **9.34** (9.24-9.37) | 43.59 / 40.66 | 1,044 / 889 |

Counts (`log/`, one log-on run per cell; compiles over all vCPUs; fexc CPU from the servers' `wait4`):

| Run | compiles before | compiles after | memory hits after (from another vCPU) | simultaneous duplicates | kept MB before / after | fexc CPU s before / after |
| --- | --- | --- | --- | --- | --- | --- |
| Installer N = 1 | 126.4 k | 126.8 k | 193.6 k (0) | 0 | 82 / 82 | 1.69 / 1.72 |
| Installer N = 2 | 198.6 k | 129.6 k | 253.5 k (151.6 k) | 513 (0.40%) | 128 / 84 | 2.79 / 1.82 |
| Installer N = 3 | 254.6 k | 130.7 k | 301.1 k (226.4 k) | 1,206 (0.92%) | 164 / 84 | 3.51 / 1.84 |
| Installer N = 4 | 307.6 k | 130.9 k | 317.6 k (261.0 k) | 1,415 (1.08%) | 199 / 84 | 4.35 / 1.87 |
| `build` N = 4 | 724.1 k | 248.2 k | 1,052.7 k (886.4 k) | 13,979 (5.63%) | 462 / 158 | 10.61 / 3.50 |
| `hackbench` N = 4 | 179.6 k | 85.9 k | 122.9 k (103.9 k) | 1,298 (1.51%) | 117 / 56 | 2.55 / 1.22 |

Lock waits are negligible: installer N = 4 526 waits, 1.1 ms in all (the worst shard 0.11 ms); `build` N = 4 2,452 waits, 5.4 ms
(0.47 ms). **Ship threshold met**: installer N = 4 -2.54 s (-28%), `build` N = 4 -3.69 s (-28%), `hackbench` N = 4 -0.26 s (-2.7%,
ranges apart), N = 1 unchanged within its spread; process CPU -4.7 s and -12.1 s at N = 4, max RSS -281 and -690 MB. At N = 4 the
installer now compiles what N = 1 compiles; it is faster than N = 1 (6.6 against 8.7 s; QEMU multi 4.81 s).

**Step 2, an in-flight table: built, measured, not kept** (`build`'s simultaneous duplicates passed 5%). A lookup that found nothing
registered a flight (limit, half and offset, all gating values, the first 64 bytes) under the shard lock; a vCPU missing alike
waited on it (condvar; the flight landed when its answer was kept or on any error), then looked again. Measured (`campaign2.py`,
`table2.txt`, `inst2/`, `runs2/`, step 1 against step 2 interleaved, three rounds, the contended runs of another agent's VM
dropped): installer N = 4 6.79 (6.62-6.86) against 7.01 (6.75-7.11); `build` N = 4 9.41 (9.40-9.80) against 9.49 (9.43-9.55,
two); `hackbench` N = 4 9.48 (9.34-9.62, two) against 9.42 (9.36-9.67). Duplicates fell to 0 everywhere, and every one of
`build`'s 15,767 waits ended in a hit, but a wait (22.8 us average, 359 ms in all) costs what the compile it saves does
(compiles 248.2 k to 233.8 k, fexc CPU 3.50 to 3.36 s): no wall gain, so it was reverted (`step2-full.patch`); the shipped tree is
step 1 (its stub's code is byte-identical to the measured one).

**Checks** (`suite/`, `suite13.sh`, series49-sha, the final tree, one VM runner at a time, each case's output deleted as it ended):
`cargo test` 168 + 1 ignored, also with `FEXC` set (4 new); fixtures 64 and seed 44 at both block sizes; faults 7,912 at both;
`run_slice3_vm.py` `contain` (423 controls), `smp`, `sharedpt`, `coherence` 0 failures; `run_k1.py` 296 runs, no problem;
`run_boot_smoke.py` 4/4; `run_r1.py r1a` 7/7 and `r1b` 12/12 (negative controls flagged); every installer boot of the campaigns
reached the dialog and every benchmark run verified. Not rerun: the rest of the standing suite (the change is in the host's
answer map, which only `boot-linux` shares; the program driver's runs above use it per vCPU).

**Independent review** (read-only, of the diff): no blocking defect (key complete, one lock per operation, containment
unchanged). Applied: a flip's dropped generation (up to 16 k answers) is freed after the shard is unlocked; the kept totals moved
from every vCPU line to the run's `answers` line; the concurrency test now puts every RIP in one shard with distinct keys, so its
four threads look up across at least four flips of that hot shard, hold a reference across them, and the bound (two generations)
and the evictions are asserted (before, its bound could not fail and a flip depended on the hash). Re-measured for the per-shard
bounds (a sixteenth of the old per-vCPU bound; `runs3/`, `log3/`, `table3.txt`): `build` N = 1, base against the reviewed tree,
three interleaved rounds: 19.76 (19.73-19.77) against 19.78 (19.65-19.91) s, process CPU 23.07 against 23.06 s, max RSS 1,300
against 1,117 MB; compiles 229.8 k both (no extra compile from hot-shard flips). `cargo test` 168 + 1 ignored, also with `FEXC`.

### Measured (chunk 14: multi-core costs, November 4; uncommitted on edfcee9, series52-adox66)

**Measurement** (`natemu-raw/mcore-20261104-01/`: `bench.py` now samples `proc_pid_rusage` v6 at each marker (CPU, CPU on
performance cores, runnable time; `tools/prusage.c`, `usage.py`); the perf log gains a 5 ms per-vCPU bucket sampler (`snap`
lines, `snaps.py` cuts them at the markers) and the perf resident a wake record (`resident.wakes`: post to the end of `wfi`,
post to the end of the park, own timer expiry to the end of the park; the sender stamps `Events::rung`). Diagnosis runs at HEAD
0df11b8/edfcee9 with series49-sha (`meas/`, `perf/`, `try/`), campaign with series52-adox66.)
1. **Wakes and halted time.** The phase-2 finding of 3 s halted in `zstd` N = 4 was the boot. Within START-END, `hackbench` and
   `locks` N = 4 halt 0.08-0.28 s of 6.4-9.7 s per vCPU; `zstd` compress 0.19-0.24 of 0.73 s (busybox `time`: 74% of the vCPUs
   busy; zstd's own serial work), decompress and verify run one thread. Parks are 500-900 per vCPU per run, boot included; a
   post wakes the vCPU in 8-10 us average (97% under 16 us; max 77 us) and the event is taken 12-15 us after the post (max 3.4 ms
   once); the timer wakes 5-7 us late. At most about 10 ms per vCPU per run: no faster wakeup path was built, and no halt
   polling (it would spin host cores for the same 10 ms).
2. **Core placement.** Without a QoS class (machine otherwise idle), vCPU threads spend 93-100% of their CPU on performance
   cores and wait runnable 0.01-0.33 s per run; the fexc servers 93-97%. With `QOS_CLASS_USER_INTERACTIVE` on the vCPU threads
   (three interleaved runs, series49-sha, `qos/`): `locks` N = 4 5.00 (4.82-5.06) -> 4.79 (4.69-4.81) s, `zstd` N = 4 1.18
   (1.16-1.18) -> 1.14 (1.09-1.17), `build` N = 4 9.36 (9.26-9.37) -> 9.32 (9.23-9.67); vCPUs 100% on performance cores, while
   one `build` run moved its servers to 86%. Servers keep the default class: a vCPU waits for its server's reply, so a lower class
   only delays it (and std's `Command` cannot set a spawn QoS; a raise is not measured).
3. **`locks`.** Correct: the program checks both counters (mutex-protected and atomic) and exits 1 on a mismatch; every run
   verified. MONITOR/MWAIT are not advertised (CPUID.1:ECX[3] = 0: the guest idles with HLT); PAUSE is Arm `yield` (a hint, no
   exit). It speeds up with N because at N = 1 6.4 of 11.2 s are guest system time: 3.1 M `sched_yield` calls (one per 64
   rounds) at about 2 us each (QEMU 0.8 us), which divide over the vCPUs. Contention is there and costs: guest CPU 11.2 s at
   N = 1, 21.2 s at N = 4 (user 4.7 -> 10.2 s, futex system time 6.4 -> 10.9 s). Natively and under QEMU the same contention
   costs several times their base per-round time; here the system-call path dominates. No hidden defect; the real cost is the
   syscall path (single-thread work).
4. **`hackbench` N = 4** is CPU-bound (natemu CPU 37.7 s in 9.6 s: 3.9 cores). Per vCPU (perf resident, 9.7 s): generated code
   3.3 s, linker entries 1.76, `enter` 1.0, system instructions 1.0, shadow walks 0.5 and aborts 0.45, lookup 0.48, miss 0.37,
   TLB 0.39; N = 1 has the same buckets. The N = 4 excess is shadow misses after CR3 loads: 2.2-2.5 M per vCPU (5.6 M at N = 1),
   about 130 per CR3 load, and **94% of them supervisor accesses to the upper half** (a temporary count by space and half:
   kernel pages, global under `nopti`), because a CR3 load dropped every data shadow.

**Built.**
1. **Global shadows kept across CR3 loads** (abi `shadow.rs`, runtime `tables.rs`, `execution.rs`). A CR3 load without PCID
   keeps global TLB entries (SDM Vol.3 4.10.4.1). The shadow pool now has two ends: the supervisor space's upper half (the kept
   half) takes its tables from the top, everything else from the bottom. `Space::map_global` installs the shadow of a global
   translation (leaf G with CR4.PGE, from the data walk); any other install in the kept half (`map`, the all-ones page included)
   sets a word that makes the next CR3 load a full reset. `Space::reset_cr3` (from `tlb(s, false)`, the CR3 load) then drops both
   lower halves and the user space's upper half, returns the bottom of the pool and invalidates both ASIDs; the kept half
   stays. Everything else is a full reset as before (paging-control changes including PGE, MTRR writes, capacity, a peer's
   request, the APIC base move); INVLPG reaches kept shadows (`unmap_large`), and a #PF now unmaps CR2's shadow in both spaces
   (x86 drops its TLB entries; before, the next CR3 load did). A kept reset leaves the vCPU's writable bits (a superset of its
   writable shadows) and does not move its reset counter, so a peer marking a page waits for a real full reset. Counter: K1
   `kept_resets`. Effect (`try/`, one log run each, `hackbench` N = 4): shadow retries 2.2-2.4 M -> 0.20-0.27 M per vCPU, every
   CR3 load kept, shootdowns 1-4 -> 5-8 per vCPU (stale writable bits), no evictions; `build` N = 4 retries -30%.
2. **vCPU threads at the user-interactive QoS class** (`linux.rs`, the `boot-linux` body; item 2 above).
3. The measurement above (perf log and perf resident only; the host stamps `rung` only with the log on).

**Tests** (abi `shadow.rs`): kept half survives `reset_cr3` while lower halves, the user space's upper half and global lower
shadows go; reuse of the bottom never meets the kept tables; a non-global kept-half shadow makes the next load full, then
keeping resumes; INVLPG reaches a kept shadow; a full reset returns the top and clears the word; both ends share the
capacity; a link into a returned page is corrupt. The existing pool test now expects the kept half's tables at the top.

**Measured** (`campaign.sh`, `summ.py`, `table.txt`, `runs/`, `inst/`; the `natemu` stub, cache off, `mitigations=off nokaslr`,
2 GiB, series52-adox66, base (edfcee9) and c14 interleaved, three rounds, one VM at a time, every run verified or reached and
uncontended; median (min-max), seconds):

| Benchmark | N | before s | after s | change | CPU s base / c14 |
| --- | --- | --- | --- | --- | --- |
| installer | 1 | 8.65 (8.32-8.76) | 8.05 (8.04-8.30) | -6.9% | 8.7 / 8.1 |
| installer | 4 | 6.67 (6.46-6.81) | 6.44 (6.38-6.65) | -3.4% | 10.5 / 10.0 |
| zstd | 1 | 2.13 (2.12-2.14) | 2.13 (2.07-2.16) | +0.1% | 5.6 / 5.4 |
| zstd | 2 | 1.45 (1.43-1.49) | 1.42 (1.36-1.46) | -2.1% | 5.8 / 5.7 |
| zstd | 3 | 1.28 (1.27-1.32) | 1.27 (1.26-1.28) | -0.9% | 6.2 / 6.1 |
| zstd | 4 | 1.24 (1.17-1.28) | 1.14 (1.12-1.18) | -7.4% | 6.6 / 6.5 |
| hackbench | 1 | 30.32 (29.96-30.42) | 28.27 (28.20-28.36) | -6.8% | 33.7 / 31.5 |
| hackbench | 2 | 16.17 (16.15-16.32) | 14.86 (14.75-14.89) | -8.1% | 35.6 / 33.0 |
| hackbench | 3 | 11.55 (11.33-11.75) | 10.39 (10.34-10.46) | -10.0% | 38.1 / 34.6 |
| hackbench | 4 | 9.41 (9.29-9.71) | 8.45 (8.39-8.46) | -10.2% | 41.0 / 37.2 |
| locks | 1 | 11.01 (10.98-11.04) | 10.97 (10.97-11.04) | -0.3% | 14.4 / 14.2 |
| locks | 2 | 5.87 (5.82-5.90) | 5.83 (5.77-5.94) | -0.8% | 15.0 / 14.9 |
| locks | 3 | 4.55 (4.50-4.68) | 4.60 (4.53-4.82) | +1.1% | 17.1 / 17.1 |
| locks | 4 | 5.00 (4.69-5.01) | 4.71 (4.64-4.97) | -5.9% | 23.2 / 22.2 |
| build | 1 | 20.67 (20.41-20.80) | 20.29 (20.29-20.33) | -1.9% | 24.2 / 23.6 |
| build | 2 | 13.11 (13.11-13.37) | 13.08 (13.04-13.21) | -0.2% | 27.1 / 27.0 |
| build | 3 | 10.46 (10.43-10.60) | 10.31 (10.24-10.68) | -1.4% | 29.6 / 29.1 |
| build | 4 | 9.61 (9.34-9.62) | 9.23 (9.12-9.55) | -4.0% | 32.2 / 31.7 |

**Ship threshold met**: `hackbench` -6.8% at N = 1 to -10.2% at N = 4, the installer N = 1 -6.9% and N = 4 -3.4%, `zstd` N = 4
-7.4%, `locks` N = 4 -5.9%, `build` N = 4 -4.0%, each with ranges apart or nearly; N = 1 elsewhere unchanged within spread (`build`
-1.9%, `zstd` +0.1%, `locks` -0.3%); `locks` N = 3 +1.1% and `build` N = 3 -1.4% within spread. QEMU's control flags gained
`+smep,+umip` in `bench-20261103-01/scripts/bench.py` and `qcmp-20261103-01/boot.py` (no QEMU run in this chunk).

**The final tree** (after the review's changes below; `confirm.sh`, `runs2/`, `inst2/`, three runs each): installer N = 1 8.19
(8.08-8.22), N = 4 6.43 (6.41-6.52); `hackbench` N = 1 28.40 (28.19-28.50), N = 4 8.41 (8.40-8.46); `build` N = 1 20.20
(20.17-20.22), N = 4 9.20 (8.99-9.26); `locks` N = 4 4.84 (4.71-5.04); `zstd` N = 4 1.20 (1.19-1.25): as measured above within
spread (`zstd` N = 4 is the noisiest cell).

**Checks** (`suite14.sh`, `suite/`, the final tree, series52-adox66, one VM runner at a time, each case's output deleted as it
ended): `cargo test` 171 + 1 ignored, also with `FEXC` set; the resident stack audit; `run_x3.py` 54/54 cases; `run_x4.py` 33
units, 28 adversarial, 6 trampoline; `run_l4a.py`, `run_l4b.py` all ok; `run_slice3_vm.py` `smp`, `events`, `kicks`, `delivery`,
`sharedpt`, `coherence` 0 failures; `run_k1.py` 296 runs, no problem; `run_boot_smoke.py` 4/4; `run_r1.py r1b` 12/12 (negative
controls flagged); every campaign boot reached and every benchmark verified.

**Independent review** (read-only, of the diff): no blocking defect (every flush path, the writable-bit protocol, pool links and
the ASID-wide invalidation checked). Applied: the #PF unmap of CR2's shadow, checked arithmetic in the pool's end checks, the
host's `rung` stamp only with the log on, two more tests (INVLPG of a kept shadow, a full reset returning the top). Noted: stale
writable bits can force a peer's shootdown and a full reset where a CR3 load used to clear them (measured: 5-8 per vCPU per run).

**Next** (from the evidence): the syscall path (`locks` N = 1 sys 2 us per `sched_yield`, `hackbench`'s linker/entry/system
buckets), single-thread codegen (`zstd`'s decompress 0.16-0.24 s and verify 0.28 s against 0.07-0.09 natively), then PCID.

### Measured (compile-ahead gate, November 4; uncommitted on 1d2719c, series52-adox66)

The gate before building compile-ahead (design: a host pool of fexc servers compiling the exits of each admitted block
ahead into the shared answers; version 1 only targets on pages the request fetched; 2 workers, a 64-job newest-first
stack, depth 2-3). **Log change:** the request line gains the request's start, a step flag, the block's guest bytes, a call
flag and the block's exit candidates with an on-fetched-page flag (handoff, "Performance log"); computed only with the log
on (`Admitted::last` is the one field added outside it). **Runs** (`natemu-raw/ahead-20261104-01/`, the `natemu` stub,
cache off, log on, one VM at a time): installer N = 1 8.31 s and N = 4 6.67 s, `build` N = 1 20.02 s and N = 4 9.33 s.
**fexc on an efficiency core** (the installer N = 1 compile stream, 126 k requests, replayed through one server, wall per
compile with the pipe): 16 us at default or utility on an idle machine (performance cores), 32 us at utility with the four
performance cores busy, 77 us at background (two at once 82 us).

**Analysis** (`ahead.py`; addresses guest-physical, as answers are kept by bytes):
- 82-88% of compiles are depth-1 successors of an earlier compiled block, on its fetched pages; 18-23% of candidates lie off
  them. But the successor is asked for 4.0-4.4 us (median) after its predecessor's reply; only 26-29% have more than 15 us.
  Counting the earliest predecessor at depth <= 3 the median lead is 67-85 us, and 86-89% have more than 30 us.
- Simulated pool, share of compiles served before they were asked for (fexc time saved on the vCPUs' path):

| Run (worker cost) | depth 1 | depth 2 | depth 3 | ahead compiles wasted, depth 3 |
| --- | --- | --- | --- | --- |
| installer N = 1 (16 us) | 24.4% (0.49 s) | 35.3% (0.72 s) | 45.4% (0.92 s) | 80 k of 138 k |
| installer N = 4 (32 us) | 20.6% (0.49 s) | 28.7% (0.68 s) | 34.2% (0.80 s) | 69 k of 114 k |
| `build` N = 1 (16 us) | 23.2% (0.85 s) | 33.2% (1.21 s) | 43.4% (1.58 s) | 141 k of 240 k |
| `build` N = 4 (32 us) | 19.3% (0.95 s) | 26.8% (1.31 s) | 32.2% (1.56 s) | 117 k of 197 k |
| any, background (77 us) | 14-15% | 18-20% | 20-23% | |

**Gate passed** at depth 2-3 with the workers at utility QoS: installer N = 1 35-45% served, 0.72-0.92 s of its 2.06 s of fexc
round trips (9-11% of the run); `build` N = 1 33-43%, 1.2-1.6 s (6-8%); at N = 4 27-34%, 0.7-1.6 s summed over the vCPUs
(roughly a quarter of that in wall time, spread over four vCPUs). It fails at depth 1 and at background QoS: the design's background class at N >= 3
must be utility. 58-61% of ahead compiles are wasted (at 16 us about 1.3 s of extra fexc CPU and 50 MB of answers for the
installer). 35-48 k compiles a run are *late* (the worker had started); a vCPU that waited for an in-flight ahead compile
instead of compiling would recover part of them. Limits: the recorded timeline (a faster vCPU shortens the leads:
optimistic); bytes and gating taken equal at the same guest-physical address (answers at a re-requested address, 1-7% of
compiles, are never counted as served: conservative); four busy loops stood in for the vCPUs in the cost replay; the
worker's admission (to read the links) is not timed.

### Measured (chunk 16, November 4; uncommitted on d2a3b0d, series52-adox66)

**Outcome: not shipped.** The pool missed the ship threshold, so its code was not committed; the patch is kept at
`natemu-raw/ahead-20261104-02/chunk16-pool.patch` (896 lines, against d2a3b0d) for a later ahead-of-use placement step, which
would need its own gate. `NATEMU_AHEAD` below refers to that patch, not to the committed tree.

**Built** (compile ahead, step 1 of the design: host only; `crates/host/src/execution/ahead.rs`, `execution.rs`, `linux.rs`).
A pool of `fexc --serve` workers (each its own server at the utility class through `taskpolicy -c utility`, worker threads at
`QOS_CLASS_UTILITY`, a 1 s compile deadline) per run's shared answers. After a request that compiled a block (not a step, no
limit cut, no halving), the vCPU queues the block's exits (link targets, a call's return site) that lie in the bytes it
copied (RIP through the walked pages; no guest walk, nothing marked), on a 64-job newest-first stack (a push past it drops the
oldest, a RIP already waiting is not pushed again; an idle worker only is woken). A worker skips a job whose answer is kept or
in flight, compiles the slice from the target to its page's end under the requesting vCPU's gating, arguments and block limit
then, keeps it as an ordinary entry (`ahead`, `used` marked at first use) and, below the depth, admits it only to read its
exits and queues those on the same bytes. Use is unchanged: `Entry::matches` at the vCPU's request, then relocation and
admission at its RIP with its bytes. A vCPU whose block a worker is compiling from the same bytes and gating waits for that
answer (in-flight table, at most 1 ms) instead of compiling it. Failures (fexc errors, dead servers) are dropped and counted,
never the run's; `stop` discards waiting jobs and joins the workers. Under `--check-reuse` every ahead answer used is also
compiled fresh and compared. Perf log: an `ahead` line (handoff, "Performance log"). **Off by default** (`NATEMU_AHEAD=on`, 2
workers depth 3, or `WORKERS:DEPTH`): it did not pass the ship threshold.

**Tests** (`ahead.rs`): the stack's bound, order, repeats and the stop's discard; job slices at page ends; a vCPU waits only
for the same limit, bytes and gating, until the job lands, also when the worker panics (nothing kept, it compiles), and gives
up at its bound; a worker whose server dies drops the job and restarts it next time; with `FEXC`, a worker compiles a block's
exits to depth 2, a vCPU uses them without compiling (each counted served once, each checked against a fresh compile), and
an ahead answer whose bytes changed before the request is not used.

**Measured** (`natemu-raw/ahead-20261104-02/`: `campaign.sh`, `fill.sh`, `summ.py`, `runs/`, `inst/`, `log/`, `tune/`, `qos/`;
the `natemu` stub, cache off, `mitigations=off nokaslr`, 2 GiB; base d2a3b0d and c16 (pool 2:3) interleaved, three rounds, one VM
at a time, a run another VM overlapped rerun; every boot reached and benchmark verified; median (min-max), seconds; CPU and RSS
the natemu process's):

| Benchmark | N | before s | after s | change | CPU s before / after | RSS MB before / after |
| --- | --- | --- | --- | --- | --- | --- |
| installer | 1 | 8.10 (7.74-9.27) | 8.06 (7.68-8.11) | -0.4% | 8.1 / 11.0 | 557 / 605 |
| installer | 4 | 6.54 (6.38-6.57) | 6.34 (6.22-6.40) | -3.0% | 10.1 / 13.2 | 926 / 973 |
| `build` | 1 | 20.01 (19.90-20.78) | 20.24 (19.90-20.48) | +1.1% | 23.2 / 28.3 | 1114 / 1181 |
| `build` | 4 | 9.28 (9.24-9.38) | 9.34 (9.23-9.49) | +0.7% | 31.9 / 37.6 | 1552 / 1596 |

**Ship threshold not met**: installer N = 4 -3.0% with touching ranges, the other cells within spread, `build` N = 1 slightly
slower; CPU +3-6 s a run, RSS +45-70 MB. The default is off; the code stays for step 2 (owner's call).

**Counters** (one log run per cell, `log/`):

| Run | vCPU compiles (base log) | ahead compiled | served | waited, then served (mean wait) | late | wasted | worker CPU (servers + threads) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| installer N = 1 | 57.0 k (126.6 k) | 129.7 k | 38.8 k | 30.8 k (21 us) | 14.9 k | 60 k | 2.0 + 1.4 s |
| installer N = 4 | 84.7 k (132 k, gate) | 113.0 k | 33.2 k | 13.5 k (44 us) | 20.1 k | 66 k | 3.4 + 2.3 s |
| `build` N = 1 | 99.0 k (229.6 k, gate) | 221.1 k | 67.3 k | 63.6 k (21 us) | 27.6 k | 90 k | 3.5 + 2.4 s |
| `build` N = 4 | 158.2 k (249.3 k, gate) | 201.4 k | 62.3 k | 28.8 k (41 us) | 41.8 k | 110 k | 5.9 + 4.3 s |

Waits gave up at 1 ms 3-6 times a run (longest 1.5-2.8 ms); no job failed; drops only at N = 4 (14-20 k); servers' maximum
RSS 6-7 MB; kept answers at the installer's end 187 k (117 MB). **Why the wall does not follow:** the vCPU's request time
(installer N = 1, log runs) falls only 2.95 -> 2.52 s: fexc 2.19 -> 1.20 s, but the waits add 0.65 s (lookup 0.07 -> 0.72 s) and
the compiles a vCPU still makes slow from 17.3 to 20-22 us each with any pool (1:1, 1:3, 2:2, 2:3, 4:3: 2.5-2.9 s of
requests), the servers interfering on the same cores. A wait costs about a compile (21 us): a depth-1 job starts about when
its predecessor is admitted and is asked for 4 us later; only depth 2-3 jobs land in time (31% of base compiles served at
N = 1, the gate's simulation at 32 us: 34%). Tuning (`tune/`, `qos/`, one run each): depth 2 or 3, 1-4 workers, the wait
bound 0, 30 us or 1 ms, and the default class instead of utility (waits still 22 us) all landed within the base runs' noise (7.6-8.5 s; one default-class run 9.3 s).

**Checks** (`suite/`, the final tree, one VM runner at a time, outputs deleted as each case ended): `cargo test` 176 + 1
ignored, also with `FEXC` set; the resident stack audit; `run_slice1_vm.py`, `run_slice1_faults.py` (default block size),
`run_slice3_vm.py contain` and `smp`, `run_k1.py`, `run_boot_smoke.py` 4/4, `run_r1.py r1a` and `r1b` all ok, and
`run_boot_smoke.py` and `r1b` again with `NATEMU_AHEAD=on` ok; `boot-linux --check-reuse --code-shape` installer boots with the
pool on at N = 1 and 4 reached with no mismatch (63.7 k and 69.9 k vCPU compiles; `check/`).

**Independent review** (read-only): no blocking defect (argument identity between vCPU and worker requests, slices, the
excluded cases, lock order, wakeups, shutdown checked). Applied: a long wait bound in the wait test (it could time out under
load), a 1 s worker compile deadline (a hung server would hold the run's end up to 30 s), the first failure reported on
standard error, a test comment that overclaimed. Noted: duplicate jobs are keyed by RIP and limit only (a stale job can
shadow a newer one: lost hits, not wrong answers); workers also fill the persistent cache with speculative answers; ahead
answers share the in-memory generation bounds.

**Next** (from the evidence): step 2 (placing ahead answers in the VM) saves the 4 us request on top of the compile, but only
for answers in time (about a third here) and keeps the interference; it needs its own gate on these counts first. Otherwise
the syscall path and single-thread code generation, as chunk 14 found.

### Measured (chunk 15: CLI, STI and RDTSC inline at CPL 0, November 4; uncommitted on e2f8aba, measured on 1d2719c, series54-inline-tsc)

**Measured first** (`natemu-raw/inline-20261104-01/`; the syscall design study's option (b), `meas/`): `run_k2.py --only cost` at
HEAD: CLI 0.194 us, NOP 0.002, so a full trip costs 0.19 us; `div` (a helper trip, no transaction) 0.104 us, the light-trip
floor. One perf `hackbench` N = 1 run with temporary counters (`meas/counters.diff`, not kept; 27.9 s): CLI 16.83 M, STI 16.70
M, RDTSC 16.13 M, every one at CPL 0; through the dispatcher's miss CLI 16.15 M (96%: Linux's `cli` follows a `ret`), STI
0.10 M, RDTSC 1; first instruction of what would be its block (a branch target or right after another system instruction)
CLI 16.26 M, STI 0.15 M (0.9%), RDTSC 0.11 M; STI setting IF with an event pending 1,941 (0.012%, the inline STI's slow
path); STI right before another system instruction 0. The go rule held (trip >= 0.15 us, all at CPL 0, slow path under 5%).

**Built** (fexc patches 0031, 0032 and the shim; host admission and request flags; resident; abi):
1. **CLI and STI.** A translate request at CPL 0 (never a step) passes `--cpl0` (gating argument 8, read only when a CLI or
   STI is decoded; the CPL is in `Cpu::code_context`). fexc translates the one-byte CLI anywhere: a store of 0 to IF's flag
   byte (`frame::IF`, FEX's flags[9]), which every exit and fault after a CPL-0 block banks (`bank` reads it as it reads DF,
   but only at CPL 0, so a CPL-3 store into the page, the recorded deviation, cannot change IF; `enter` writes it from the
   bank). The one-byte STI only where its shadow stays inside the block: never its block's first instruction and
   never its last (the next decoded instruction must be translated: an admitted form or an inline CLI or RDTSC, not a REP
   string or LOCK NEG, whose exits to their own RIP would put a resident boundary in the shadow; FEX's Core.cpp now passes
   it to `Cut`). An inline STI loads the resident's event-pending byte (`frame::EVENT_PENDING`, FEX's unused flags[22]): set,
   it takes a literal exit to its own RIP, where the block begins with the STI, which the host decodes and the resident runs
   as today (shadow, step, delivery); clear, it stores IF = 1. The byte is written before generated code runs: at every
   `enter` (0 when IF is set there: `find` took every event a boundary with IF set may take), every `resume` and every
   doorbell's return in `interrupted` (`events::held`: an unblocked NMI, the ExtINT or an APIC interrupt pending, one relaxed
   look at the event record in the common case). A post after the look rings the doorbell, which writes it again, or, past
   the STI, arms a stop at the block's next exit as for any event. `held` does not see a LAPIC timer expiry that is due but
   not yet polled, nor reinject's backlog (both reach the IRR at the next boundary's poll): such an interrupt, due while
   IF = 0, is taken at the first block exit after the STI's next instruction instead of right after it, later than for an
   interrupt already pending, which the SDM permits for one recognised after the STI.
2. **RDTSC.** With `--cpl0` and a host counter at 24 MHz (`clock::frequency`), `--tsc` (gating argument 9): the two-byte
   RDTSC is translated as the resident computes it: V by `isb; mrs cntvct_el0`, the resident's own read (FEX's
   self-synchronising read; CNTVCTSS_EL0 instead where the vCPU presents FEAT_ECV, which HVF's does not),
   floor(V x 125 / 3) as 125a + 41r + (r >> 1) with a = umulh(V, 0xAAAA..AB) >> 1 and r = V - 3a, plus the TSC offset
   (`frame::TSC_OFFSET`, FEX's unused `_pad1` at 0xb8, written from the TSC MSR's slot at every `enter`), EDX:EAX with the
   upper halves zero. EL0 may read the virtual count: CNTKCTL_EL1 = EL0VCTEN (was 0).
3. **Admission** (`admit(.., cpl0)`): a translated CLI, STI or RDTSC must be its plain form in a CPL-0 block, an STI neither
   first nor last nor right before a REP string or LOCK NEG (checked on the guest bytes at the instruction boundaries of
   fexc's PC table, which admission already checks against the code), and each STI's RIP is a literal-exit target; IF's byte is writable only in a CPL-0 block (with the code shape: only inside a CLI or STI); the event-pending
   byte and the TSC offset are readable, never writable; `mrs xT, cntvct_el0 | cntvctss_el0` only inside a CPL-0 block's
   RDTSC, with or without the code shape, and `isb` there too (elsewhere the code shape still refuses it). *Containment:* IF's byte is guest state the resident uses only to decide which
   events to take (as DF decides string direction); the STI's exit goes to a RIP of the block's own bytes through the
   linker's usual checks; the two new readable fields hold only this vCPU's own state; EL0VCTEN exposes only reads of
   CNTVCT_EL0, CNTVCTSS_EL0 and CNTFRQ_EL0, the virtual count (the host's `mach_absolute_time`, vtimer offset 0) that RDTSC
   already gives the guest at any CPL without CR4.TSD, scaled and offset; the physical count (EL0PCTEN), both timers'
   registers (EL0VTEN, EL0PTEN) and the event stream stay trapped (`contain` now forges `mrs cntpct`, `msr`/`mrs` of the
   virtual timer, `msr cntp_ctl` and `msr cntkctl_el1`: `trapped`; E2's blocking hole was the debug channel, MDSCR_EL1.TDCC,
   unchanged). A CPL-3 RDTSC (CR4.TSD) and RDTSCP stay the resident's.

Per x86 rule (SDM Vol.2 CLI, STI, RDTSC; Vol.3 7.8.3, 7.9 Table 7-2): at CPL 0 neither CLI nor STI faults (CPL <= IOPL; no
VME/PVI in 64-bit mode) and RDTSC's TSD check does not apply; prefixed forms (LOCK: #UD) stay the resident's. STI's shadow
only when it sets IF: an event pending at an inline STI exits there and the resident's STI sets the shadow, steps the next
instruction and delivers after it; an STI after a MOV SS, or as a branch target, starts its block and is the resident's;
STI; HLT and STI; MOV SS keep STI the resident's (the next instruction is a cut); TF: blocks never run with TF (every
instruction is a step, and steps are compiled without `--cpl0`); instruction breakpoints cut the bytes, so an STI before one
is last and the resident's. An event arriving while IF = 0 inside a block is held (banked IF = 0 at the stop) and either
marked for the block's STI or taken at its next exit after IF is set: never inside the shadow, never with IF = 0.

**Tests.** Host: `cpl0_translates_cli_and_sti_inline` (with `FEXC`: CLI first or not, STI mid-block with its exit link point,
STI first, before HLT, a REP string, LOCK NEG, last, or prefixed: the resident's; RDTSC with `--tsc` only, not with REX; a
CPL-3 request cuts both and never takes the CPL-0 answer; admission refuses the CPL-0 objects outside a CPL-0 block),
`interrupt_flag_forms` (with STI before REP STOSB and before LOCK NEG), `cpl0_sti_exit_is_relocated` (with `FEXC`: a
CPL-0 block with an STI exit and a RIP-relative LEA is fexc's object at another RIP after the host's relocator and after
the resident's re-check, link points included), the frame fields and counter words in `guest_state_fields_and_i1_words` (refused, with and without
the code shape, outside an RDTSC); abi `inline_tsc_formula_is_exact` (patch 0032's arithmetic against `clock::tsc` for 10^6
random and every edge V) and the layout test. `run_slice3_vm.py events`: `ev-cli-inline` (an inline CLI holds an
interrupt raised after it; the inline STI mid-block takes it after the next instruction) and `ev-sti-cli` (STI; CLI with an
interrupt pending: held through the CLI, taken after the next STI's shadow) at 1, 2, 4 vCPUs against QEMU and
`delivery_model`; x2 `x2-cpl3-if-byte` (review: a CPL-3 store of 0 to IF's byte, then CPUID: PUSHFQ sees IF = 1 and an
interrupt posted after it is taken; red with `bank` reading the byte at every CPL: IF = 0, the interrupt never taken); the
existing STI programs (`ev-sti-shadow`, `-ret`, `-jmp`, `-nops`, `ev-hlt-wake`, `ev-mov-ss*`) now
run the inline forms; stress `ev-window` (2,000 random-time events into 24-instruction IF = 0 windows inside one block at
1-4 vCPUs, judged by `stress_check`: never in the shadow, never with IF = 0, none lost; QEMU equal without events; at the
default block size 7-15 of about 100 deliveries per run came through an inline STI's exit); x2 `x2-cli-sti-pushf-cpl0`.
`run_k2.py tsc` (inline RDTSC alternating with the resident's RDTSCP never decreases and all values lie on one lattice
floor(125V/3) + offset; WRMSR TSC then RDTSC; a token ring at 2-4 vCPUs never sees the TSC go back; no RDTSC decode) and a
`nop_sti` cost row; `coherence` and `nopruns` now probe a 66-prefixed CLI/STI (the resident's at CPL 0). `contain` forges
the counter and timer words above. Negative controls (scratch builds, then reverted): the pending byte always 0: 15 of the
21 event runs red (`ev-sti-shadow`, `-ret`, `-jmp`, `ev-cli-inline`, `ev-sti-cli`); `bank` ignoring IF's byte: 12 red
(`ev-sti-ret`, `-jmp`, `ev-cli-inline`, `ev-sti-nops`); the TSC offset not written: `tsc one` red (2,000 decreases, the WRMSR
value); patch 0032 with `r >> 2` for `r >> 1`: `tsc one` red (off the lattice). Not red by construction: a doorbell inside a
block not writing the pending byte (the event is then taken at the block's next exit, which the SDM also permits for an
event that arrives after the STI): it only moves deliveries later (`ev-window`'s inline-STI exits).

**Measured** (`natemu-raw/inline-20261104-01/`: `campaign.sh`, `run1.sh`, `summ15.py`, `runs/`, `inst/`, `k2/`; the `natemu`
stub, cache off, `mitigations=off nokaslr`, 2 GiB, base (1d2719c, series52-adox66) and chunk 15 (series54-inline-tsc)
interleaved, three rounds, one VM at a time, every run verified or reached and uncontended; median (min-max), seconds):

| Benchmark | N | before | after | change | CPU s before / after | QEMU multi |
| --- | --- | --- | --- | --- | --- | --- |
| `hackbench` | 1 | 28.59 (28.44-28.99) | **16.87** (16.87-16.87) | -41.0% | 31.8 / 20.0 | 15.7 |
| | 4 | 8.36 (8.35-8.37) | **4.79** (4.73-5.38) | -42.7% | 36.8 / 22.5 | 5.3 |
| `locks` | 1 | 11.03 (11.01-11.05) | **6.24** (6.22-6.26) | -43.4% | 14.2 / 9.4 | 10.7 |
| | 4 | 4.84 (4.83-4.94) | 7.17 (7.01-7.27) | +48.1% | 23.0 / 31.8 | 26.9 |
| Installer, cache off | 1 | 8.25 (8.22-8.60) | **7.90** (7.84-8.35) | -4.2% | 8.3 / 7.9 | 6.10 |

`run_k2.py cost` (three runs each, us per iteration): CLI 0.196 -> 0.000, RDTSC 0.227 -> 0.000 (18 ns in one run alone: the
counter read), STI after a NOP 0.255 -> 0.000; STI starting its block (the resident's either way) 0.227 -> 0.225; SWAPGS
0.212 -> 0.225 (each entry with IF = 0 now looks at the event record: about 13 ns); CPUID, WRMSR, POPF, `div` unchanged.

**`locks` N = 4 is slower because the guest's threads contend more, as they do natively; no interrupt or TSC defect**
(`natemu-raw/locks-20261104-01/`: `lk.sh`, `lkstat.py`, `bisect.sh`, `delay.sh`, `summ.py` and `summary.txt`, `native.sh`
and `native.txt`, `probe/`, `runs/`; the perf resident, one VM at a time, an initramfs whose `/init` also prints the guest's
`/proc/interrupts` and context-switch count around the timed span, `mkov.sh`; median (min-max) of three runs, seconds).
Work fixed (200 M rounds, a `sched_yield` every 64) and verified. Futex calls: the guest's SYSCALL count less the 3,125,922
of a one-thread run (the `sched_yield` calls and boot), per 1,000 rounds. The two switches leave out fexc's `--cpl0` (CLI
and STI stay the resident's) or `--tsc` (RDTSC does) in this chunk's tree; the waits are a busy wait in the resident at every
SYSCALL, with all three forms inline (scratch builds, not kept):

| `locks`, N = 4 | wall | futex calls / 1,000 rounds | guest remote-wakeup IPIs | guest context switches |
| --- | --- | --- | --- | --- |
| before (1d2719c) | 4.49 (4.33-4.79) | 15.1 (13.9-19.2) | 1,018 | 3,682 |
| this chunk, none inline | 4.76 (4.72-4.86) | 18.4 (17.2-18.7) | 1,313 | 3,655 |
| RDTSC inline only | 5.16 (4.84-5.23) | 30.3 (27.4-32.4) | 3,428 | 7,011 |
| CLI and STI inline only | 6.58 (6.52-6.67) | 64.4 (62.2-65.5) | 24,160 | 48,550 |
| all three (this chunk) | 7.64 (7.52-7.68) | 102 (101.6-103.1) | 56,250 | 112,700 |
| all three, 0.5 us wait at every SYSCALL | 7.08 (6.74-7.42) | 59.4 (55.6-63.6) | 16,450 | 33,020 |
| all three, 1.0 us wait | 7.04 (6.99-7.08) | 41.8 (41.5-43.0) | 7,536 | 16,400 |
| all three, 1.5 us wait | 5.94 (5.93-6.50) | 21.6 (21.4-26.4) | 3,698 | 8,274 |
| all three, 2.0 us wait (one run) | 5.15 | 11.4 | 1,826 | 3,761 |
| native, AMD reference, CPUs 0-3 | 4.17 (4.17-4.18) | not countable in user mode | 568,600 | 1,202,700 |

The extra interrupts are function-call IPIs, which Linux sends to wake a task on another CPU: they follow the context
switches (about one per two). In every natemu row timer interrupts ran at the guest's tick rate (250 per second per vCPU)
and rescheduling IPIs stayed at 21-28 a run. At N = 1 the 1.5 us wait gives 10.48 s, against 6.17 s with all three inline and 10.76 s
with none: it adds back about what the inline forms save per system call, and at N = 4 the count then falls to 21.6, near the
none-inline 18.4, with every inline form still in place. RDTSC alone, with CLI and STI the resident's, raises the count too.
In one run each, halted time was 0.07-0.18 s per vCPU in both trees; with all three inline each vCPU spent 3.1 s in
generated code (1.9 s with none) and 1.1 s in system instructions (0.5 s), for 23.5 M guest system calls (6.9 M).

The native comparison was wrong before: voluntary context switches count only the futex waits that sleep. A probe with the
same loop and glibc's low-level lock written out and counted (`probe/locks2.c`, one static binary built on the AMD machine;
in the guest its futex count equals the resident's SYSCALL count, 72.08 against 72.1), per 1,000 rounds at 4 threads:

| `locks2`, N = 4 | guest time s | slow-path entries | futex calls | waits that slept | waits returning EAGAIN | wakes |
| --- | --- | --- | --- | --- | --- | --- |
| native (three runs) | 3.92-3.95 | 59.3-59.6 | 145.6-146.3 | 2.18-2.21 | 50.9-51.2 | 92.5-92.9 |
| natemu before (1d2719c, two runs) | 4.57, 4.74 | 6.7, 7.6 | 20.6, 21.9 | 0.01, 0.02 | 9.7, 10.2 | 10.9, 11.7 |
| natemu this chunk, none inline (two) | 4.22, 4.36 | 5.8, 6.1 | 16.7, 18.2 | 0.01, 0.01 | 7.7, 8.5 | 9.0, 9.7 |
| natemu this chunk (two) | 5.45, 6.01 | 21.5, 23.6 | 72.1, 82.7 | 0.17, 0.19 | 33.8, 38.8 | 38.1, 43.8 |

Natively `locks` itself makes 600 k voluntary switches a run (`locks2` 437 k) and 568 k remote-wakeup IPIs (`locks2`
413 k). So every count moved toward native, which contends more than natemu still does; the benchmark's default mutex has
no spin phase, and neither has the probe's lock. Nothing in this chunk was changed for it. The cell stays slower: about 17 M
more futex calls a run, each through natemu's system-call path (SYSCALL and SYSRET are still resident trips).

The wait test alone does not tell contention from late interrupt delivery (a slower SYSCALL lowers the count under both);
the probe does: only 0.17-0.19 of the futex calls per 1,000 rounds sleep, and the rise is in waits that return EAGAIN at
once and wakes that find no waiter, neither of which involves an interrupt; native shows the same shape at a higher level.
The "native" rows are the AMD reference machine, a 4-vCPU KVM guest, not bare metal.

**Independent replication** (`natemu-raw/locks-20261104-01/replication/`: own builds of e2f8aba plus this patch and the
switch diff, one VM at a time): native `locks` 4.16-4.21 s, `locks2` 145.6-146.0 futex calls per 1,000 rounds; counted
natively with `perf stat` tracepoints, `sched_yield` is exactly 3,125,000 and futex 95.9 (`locks`) and 100.2 (`locks2`,
its own count 100.17) per 1,000 rounds, tracing itself lowering the count from 146. natemu N = 4, three runs each: none
inline 4.71 s, 18.1; all three 7.41 s, 100.2; all three with the 1.5 us wait 5.86 s, 21.4; timer interrupts at the tick
rate, rescheduling IPIs about 25, function-call IPIs about one per two context switches (native about 1:2.1). No flaw found.

**Checks** (`suite15.sh`, `suite/`, the final tree, series54-inline-tsc, one VM runner at a time, each case's output deleted as
it ended): `cargo test` 104 + 1 ignored (host), abi 65, also with `FEXC` set; the resident stack audit `ok`; the Python unit
tests; fixtures 64 and seed 44 at both block sizes; the i7 corpus at CPL 0 (`--cpl 0`) 69 at both; faults 7,912 at both;
`run_k1.py` no problem; `run_k2.py` all ok (`tsc` included); `run_slice3_vm.py` `delivery`, `events`, `contain`, `smp` 0
failures, `system` 1: `sysfault-cr4-umip` ("the model expects no fault"), which fails the same at 1d2719c (UMIP advertised
since edfcee9; not this chunk's); `run_x3.py` 54/54 and every group, `run_x4.py` 33/28/6, `run_l4a.py`, `run_l4b.py`,
`run_s4.py` all ok; `run_boot_smoke.py` 4/4; `run_r1.py r1a` 7/7, `r1b` 12/12 (negative controls flagged); FEX ASM 2,270:
1,898 pass, 48 precision deviations, 324 unsupported, no failure. The first pass found the code-shape check refusing the
`isb` of an inline RDTSC on a vCPU without FEAT_ECV (`system`'s `profile`, `x3`, `x4`, `l4a`); admission now allows it there
and the cases from `system` on were run again (the stub, which runs without the code shape, and so the campaign, were
unaffected).

**Independent review** (read-only, of the diff): one blocking defect, fixed: `bank` read IF's byte after every block, so a
CPL-3 store into the guest-state page (the recorded deviation) could set or clear IF; it now reads it only at CPL 0 (test
`x2-cpl3-if-byte` and its negative control above; the deviation record in abi `GUEST_STATE_VA` and the boot-features plan
now says a CPL-0 store acts as CLI/STI and what CPL 3 can read). Also applied: admission refuses an STI right before a REP
string or LOCK NEG; `held`'s timer caveat stated; a CPL-0 relocation test; the `locks` comparison with native counts (since replaced by the measured one above).
Rerun on the fixes, rebased on e2f8aba (`review/`): `cargo test` with `FEXC` (105 + 1 ignored, abi 65), the stack audit,
`run_k2.py`, `events` (the new case included), `contain` and `system` (0 failures: e2f8aba dropped the stale UMIP case), and
`--check-reuse --code-shape` installer boots, cache off: N = 1 852,075 and N = 4 219,815 resident copies checked, no halt,
both reached the dialog.

**Not done.** RDTSCP stays the resident's (TSC_AUX); a CPL-3 RDTSC too (CR4.TSD). Each resident entry with IF = 0 now pays
about 13 ns for the event-pending look (SWAPGS's cost row); the remaining trips per system call are SYSCALL and SYSRET
(design study option (c+) next).

### Measured (chunk 17: SWAPGS inline at CPL 0, November 4; uncommitted on 114c9eb, series55-swapgs)

**Measured first** (`natemu-raw/sysfast2-20261104-01/meas/`; HEAD 114c9eb, series54-inline-tsc). `rtcost.py` (one vCPU, the
slope of the driver's wall time as `run_k2.py cost`): a SYSCALL -> kernel -> SYSRET round trip from a CPL-3 loop costs
0.69 us with a kernel side shaped as Linux's (`endbr64; swapgs; ...; swapgs; 7-byte NOP; sysretq`: two resident entries
carrying six system instructions: SYSCALL, NOP run, SWAPGS; SWAPGS, NOP run, SYSRET), 0.29 us with a bare `sysretq` (one
entry, SYSCALL and SYSRET chained), 0.39 us with `swapgs; swapgs; sysretq` (one entry, four chained). So an entry costs
about 0.2 us and each instruction chained into it about 0.05 us. One perf `hackbench` N = 1 run (16.0 s; whole run, boot
included): 16.0 M SYSCALL, 16.0 M SYSRET, 32.1 M SWAPGS and 32.0 M NOP runs; 33.7 M linker entries (32.0 M of them the
two trips of each system call), 34.7 M block entries, 32.0 M L1 context switches with 14 slot writes (the switch is a slot
selection, no code write). A second run with temporary sub-buckets (`meas/subbuckets.diff`, not kept; 16.0 s), costs per
resident entry: the gate's entry kind and the linker's own work 31 ns, `load` and `bank` 23 ns, `block_at` and the
link-point checks 7 ns; per `find` pass (3.8 per entry) the boundary checks (invalidations, LAPIC poll, debug and event
checks, L1 slot selection) 12 ns and `lookup` 7 ns; per system instruction (transaction and accounting) 27 ns; in `enter`
the flag bytes, bases, TSC offset and event look 18 ns, the native frame 10 ns, the record copy 12 ns and the shadow-space
switch (`tables::select`, a CPL change at nearly every entry) 31 ns; the transactions themselves are a few ns each. A
Linux system call therefore spends about 0.58 us in the resident (2 entries x 61 ns, 8 passes x 19 ns, 6 instructions x
27 ns, 2 x 42 ns of `enter`, 2 x 31 ns of space switch) plus the gate and `eret` (in `guest`): about 9.3 s of the run's
17.9 s vCPU time. The four SWAPGS and NOP-run steps are about 0.18 us of it (4 x (27 + 19) ns), the two entries the rest.
The floor stays two entries per system call: SYSCALL and SYSRET change the CPL, so the shadow space (TTBR) and the code
context, which only EL1 can switch.

**Built** (fexc patch 0033 and the shim; host admission; resident; abi; the test driver's output):
1. **SWAPGS** (fexc series55-swapgs). A CPL-0 translate request (`--cpl0`, as for CLI and STI; never a step) translates the
   plain three-byte SWAPGS (`0f 01 f8`, no prefix) inline anywhere in a block: it loads FEX's `gs_cached` (the GS base every
   GS-prefixed access adds) and IA32_KERNEL_GS_BASE from the guest-state page and stores them exchanged. KERNEL_GS_BASE lives
   in FEX's `State.L1Pointer` (`frame::KERNEL_GS_BASE`, 0xa0, which no generated code uses). Prefixed forms (LOCK: #UD; REX,
   66) and every CPL-3 request stay the resident's (at CPL 3 SWAPGS is #GP(0)); the request key records that fexc read the
   CPL-0 argument, so a CPL-3 request never reuses a CPL-0 answer. An inline STI may now be followed by an inline SWAPGS.
2. **Resident.** `enter` writes the field from the MSR record when entering CPL-0 code and 0 at CPL 3 (a user process never
   finds the kernel's value there); `bank`, after CPL-0 code only (as for IF), reads the GS base and the field back into the
   bank and the MSR record. Every block exit, fault and stop banks through `bank`; a doorbell inside a block arms a stop and
   resumes without banking (the exit it stops at banks); a device access (`mmio::device`) banks at its site and dispatches.
   Mid-block resident work that resumes the block (helpers, divisions, shadow misses, splits) computes addresses from
   registers, never from the bank's GS base, and does not rewrite the page.
3. **Admission.** A translated SWAPGS must be the plain form in a CPL-0 block (`interrupt_flag`); 8-byte loads and stores of
   the KERNEL_GS_BASE field and 8-byte stores of the GS base are allowed only in a CPL-0 block, and with the code shape only
   inside a SWAPGS. *Containment:* a CPL-3 block can neither load nor store the field; a CPL-3 store through the guest-state
   alias (the recorded deviation) is overwritten at the next CPL-0 entry, which every CPL change passes through, and the
   bank never reads it after CPL-3 code; a CPL-0 store into either field acts as SWAPGS's writes would (without WRMSR's
   canonical check: recorded in abi `GUEST_STATE_VA` and the boot-features plan).

Per x86 rule (SDM Vol.2 SWAPGS): at CPL 0 SWAPGS cannot fault (#GP(0) needs CPL > 0; #UD only with LOCK, a prefix the inline
form excludes); it exchanges GS.base with IA32_KERNEL_GS_BASE and nothing else, is not serialising and has no interrupt
shadow, so events and stops around it need nothing new: an event arriving inside the block is taken at its next exit with
the exchanged bases banked. Blocks never run with TF (every traced instruction is stepped, and steps are compiled without
`--cpl0`); instruction breakpoints cut the block. SYSCALL and SYSRET are unchanged.

**Tests.** Host (with `FEXC`): `cpl0_translates_swapgs_inline` (first, twice in one block, after an inline STI; REX, LOCK,
66 and CPL-3 requests the resident's; admission refuses the CPL-0 object as a CPL-3 block) and `cpl0_sti_exit_is_relocated`
with an inline SWAPGS; `guest_state_fields_and_i1_words` refuses the field's loads and stores and a GS-base store in a
non-CPL-0 block, with and without the code shape, and (review) in a CPL-0 block admits them without the code shape, refuses
them with it outside a SWAPGS, and refuses 4-byte, pair and unscaled accesses to the field; `interrupt_flag_forms` (review)
refuses a SWAPGS with REX, 66 or LOCK, or in a CPL-3 block; abi layout test. `run_k2.py swapgs` (natemu, by the SDM; the driver now
reports the bank's GS base and the MSR record's KERNEL_GS_BASE): `msr` (exchange, GS:[0] loads and RDMSR of both, block
sizes default and 1), `fault` (#PF after an inline SWAPGS in the same block: the bases exchanged), `kicks` (200,000 SYSCALL
round trips under 20,000 precise stops at random times, a block boundary between the kernel's SWAPGSs: both counters
exact), `events` (4,000,000 rounds of `swapgs; inc gs:[0]; jmp; swapgs; inc gs:[8]` with IF = 1 under interrupts posted at
random times, several hundred taken: both counters exact), `contain` (CPL 3 reads the field as 0, stores a forged value, SYSCALLs:
the kernel's SWAPGS loads the real value, RDMSR reads it); every run checks that no SWAPGS was decoded. `run_k2.py cost` gains
a `syscall` row (the Linux-shaped round trip). Negative controls (scratch builds, then reverted;
`natemu-raw/sysfast2-20261104-01/nc/`): `bank` not reading the bases back: `msr` (both), `fault` and `events` red; `bank`
reading them at every CPL: `kicks` and `contain` red; `enter` writing the field at every CPL: `contain` red (CPL 3 reads the
kernel's base); `enter` not writing it: every case red; admission without the CPL-0 condition on the fields:
`guest_state_fields_and_i1_words` red; without it on the SWAPGS form: `cpl0_translates_swapgs_inline` and
`interrupt_flag_forms` red; (review) the form check without its prefix test, or the field access without its code-shape
test or its 8-byte size: `interrupt_flag_forms`, resp. `guest_state_fields_and_i1_words` red. The field access's
Unsigned-form condition is held by the size test too (a pair is 16 bytes): dropping it alone stays green. Each mutant's
output is in `nc/red-*.txt` (`nc/README.md`).

**Measured** (`natemu-raw/sysfast2-20261104-01/`: `campaign.sh`, `run1.sh`, `summ17.py`, `runs/`, `inst/`, `k2/`; the
`natemu` stub, cache off, `mitigations=off nokaslr`, 2 GiB, base (114c9eb, series54-inline-tsc) and chunk 17
(series55-swapgs) interleaved, three rounds (the installer at N = 4 once), one VM at a time, every run verified or reached
and uncontended; median (min-max), seconds):

| Benchmark | N | before | after | change | CPU s before / after | QEMU multi |
| --- | --- | --- | --- | --- | --- | --- |
| `hackbench` | 1 | 16.31 (15.64-16.33) | **13.76** (13.71-13.82) | -15.6% | 19.4 / 16.8 | 15.7 |
| | 4 | 4.96 (4.86-5.32) | **4.51** (4.28-4.99) | -9.1% | 23.2 / 21.4 | 5.3 |
| `locks` | 1 | 6.11 (5.85-6.21) | **5.58** (5.45-5.62) | -8.7% | 9.2 / 8.6 | 10.7 |
| | 4 | 7.29 (7.02-7.34) | **6.54** (6.54-6.55) | -10.3% | 32.4 / 29.2 | 26.9 |
| `build` | 1 | 19.83 (19.46-19.86) | 19.84 (19.64-19.88) | +0.1% | 22.9 / 23.0 | 28.3 |
| | 4 | 9.28 (9.10-9.40) | 9.26 (8.99-9.29) | -0.2% | 31.7 / 31.7 | 12.5 |
| Installer, cache off | 1 | 7.65 (7.47-7.86) | 7.67 (7.57-7.75) | +0.2% | 7.7 / 7.7 | 6.10 |
| | 4 (one run) | 6.31 | 6.26 | -0.7% | 9.9 / 9.6 | 4.81 |

`run_k2.py cost` (three runs each, us per iteration): SWAPGS at CPL 0 0.214 -> 0.000, the `syscall` round trip 0.738 ->
0.587; every other row unchanged (CLI, RDTSC 0.00, STI starting its block 0.224/0.225, CPUID 0.241/0.238, `div` 0.091).
Perf runs (`meas/`, whole run with boot, one each): `hackbench` N = 1 16.0 -> 13.4 s, system instructions 97.7 M -> 33.5 M
(SWAPGS and NOP runs gone, chained 64.1 M -> 17 k), `find` lookups 132.4 M -> 68.3 M, the `link` bucket 3.70 -> 2.70 s,
`system` 2.69 -> 1.57 s, `lookup` 0.98 -> 0.51 s, entries unchanged (33.7 M linker, 34.7 M block entries), generated code
6.11 -> 6.01 s; `locks` N = 1 6.0 -> 5.4 s. `build` and the installer make few system calls (their trips are elsewhere).
`locks` N = 4 is still slower than before chunk 15 (4.84 s): each system call still costs two resident entries.

**Checks** (`suite17.sh`, `suite/`, the working tree, series55-swapgs, one VM runner at a time, each case's output deleted as
it ended): `cargo test` 106 + 1 ignored (host), abi 65, also with `FEXC` set; the resident
stack audit `ok`; the Python unit tests; fixtures 64 and seed 44 at both block sizes; the i7 corpus at CPL 0 69 at both; faults 7,912
at both; `run_k1.py` no problem; `run_slice3_vm.py` `delivery`, `events`, `contain`, `smp` 0 failures; `run_x3.py` 54/54 and
every group, `run_x4.py` 33/28/6, `run_l4a.py`, `run_l4b.py`, `run_s4.py` all ok; `run_boot_smoke.py` 4/4; `run_r1.py r1a` 7/7,
`r1b` 12/12 (negative controls flagged); FEX ASM 2,270: 1,898 pass, 48 precision deviations, 324 unsupported, no failure.
The first pass (`suite/results-pass1.txt`) found two test expectations the change made stale, both fixed in the tests:
`run_k2.py coherence` case E probed a kept SWAPGS record, now inline (it now probes a REX.W SWAPGS, the resident's, as chunk 15
moved its CLI/STI probes to the 66 forms), and `system`'s `sys-msr-syscall` failed the driver's `frame_intact` check, which
allowed no non-zero byte outside the guest-state fields (it now admits the KERNEL_GS_BASE field). Rerun on the final tree
(`suite17b.sh`): `cargo test` (with `FEXC` too), the stack audit, `run_k2.py` all ok (`swapgs` included), `system`,
`delivery`, `events`, `contain`, `smp` 0 failures. `--check-reuse --code-shape` installer boots, cache off (`checkboot17.py`,
`check/`): N = 1 850,795 copies checked, N = 4 873,847 over four vCPUs, no halt, both reached the dialog.

**Not done.** SYSCALL and SYSRET stay resident trips: each Linux system call is now two entries of about 0.2 us plus the
shadow-space switch (the `syscall` row's 0.59 us). Making them lighter needs a shorter resident path (no state copy in and
out, one `find` pass), not translation: the CPL, and with it TTBR and the code context, changes only at EL1.

### Measured (chunk 18: breakdown at HEAD, November 4; analysis on 1d75dc7, series55-swapgs, no code change kept)

**Question.** Where the time goes now on the two workloads that still trail QEMU or did not move: the installer boot (cache
off, N = 1 7.67 s against QEMU 6.10 s; N = 4 6.26 against 4.81) and `build` (19.8 / 9.3 s, flat under chunks 15 and 17; QEMU
28.3 / 12.5); then what each candidate could save, from these numbers.

**Method** (`natemu-raw/breakdown-20261104-01/`, README there). HEAD's `natemu` stub, cache off, `mitigations=off nokaslr`,
2 GiB, one VM at a time; three interleaved rounds of the four cells with the perf resident and `NATEMU_PERF_LOG`, then one
uninstrumented run per cell. Window: the installer from process start to the end-point line, `build` from its START to its
END line (boot excluded), both on the log's clock (the harness now records them there). Per vCPU, the resident's buckets over
the window come from the log's 5 ms snapshots, which cover every tick from the resident's start; time outside the VM (its
`host` bucket) is split into the translation requests that began in the window (their request lines) and the rest (HVF entry
and exit, exit bookkeeping, ports, devices, the log's own lines). Counts over the window (entries by kind, CR3 loads, faults,
system instructions by kind) come from a scratch build whose snapshot thread also writes them every 100-150 ms (not kept; the
default resident of that build is byte-identical to HEAD's). Three further scratch builds counted one thing each, one run per
workload: page-fault region scans, what a dispatcher miss found in its L1 entry, and whether a larger L1 separates the two
RIPs of a conflict. fexc: the installer's compile stream recorded and replayed through one server, alone and under the macOS
`sample` profiler.

**Checks.** By chunk 1's method over each whole run, the loop's laps cover every thread's window to 0.01-0.05 ms, the two
measures of HVF entry and exit agree to 0.02-0.04 ms, and request time outside the request's named parts (reply to block,
relocation; with the log on, its bytes' hash and exit list) is 1.8-3.3% of the window, under the 5% bar. In the window table
the parts add up to the window by construction. Every `build` run verified, every installer run reached the dialog, none
contended. The log's cost: one uninstrumented run per cell gave 7.96, 6.20, 19.42 and 9.34 s against 7.73, 6.38, 19.88 and
9.22 s logged (chunk 17's medians 7.67, 6.26, 19.84, 9.26): within the spread; its request lines take 0.23, 0.35, 0.56 and
0.84 thread-seconds inside "the rest outside the VM".

| Seconds of the window, mean (min-max) of 3 runs, log on | Installer N = 1 | Installer N = 4, 4 threads | Installer N = 4, vCPU 0 | `build` N = 1 | `build` N = 4, 4 threads | `build` N = 4, vCPU 0 |
| --- | --- | --- | --- | --- | --- | --- |
| Before the resident ran (process start, image load, VM setup; APs before their start) | 0.05 (0.02-0.07) | 0.13 (0.08-0.25) | 0.03 (0.02-0.06) | 0.02 (0.00-0.03) | 0.05 (0.00-0.12) | 0.01 (0.00-0.03) |
| Host, fexc round trip (compiles) | 2.04 (2.03-2.05) | 2.39 (2.37-2.43) | 1.00 (0.85-1.17) | 2.43 (2.41-2.43) | 3.40 (3.38-3.42) | 0.95 (0.69-1.35) |
| Host, admission | 0.32 | 0.49 | 0.21 (0.17-0.24) | 0.67 (0.66-0.67) | 1.00 (0.98-1.01) | 0.28 (0.20-0.40) |
| Host, bytes copy, memory lookup, staging, keeping the answer | 0.17 | 0.25 | 0.11 (0.09-0.12) | 0.37 | 0.57 (0.56-0.58) | 0.16 (0.12-0.23) |
| Host, request outside its parts (reply to block, relocation; with the log, its hash and exit list) | 0.27 | 0.34 | 0.14 (0.12-0.17) | 0.36 | 0.48 (0.47-0.49) | 0.13 (0.10-0.19) |
| Host, the rest outside the VM: HVF entry/exit, exit bookkeeping, ports, devices, steps' rest, log lines | 0.69 | 0.99 (0.99-1.00) | 0.34 (0.31-0.37) | 1.27 (1.26-1.27) | 1.84 (1.82-1.87) | 0.51 (0.50-0.54) |
| VM, generated guest code | 1.48 (1.47-1.49) | 1.96 (1.93-1.97) | 0.55 (0.42-0.65) | 7.08 (7.02-7.14) | 8.76 (8.69-8.84) | 2.08 (1.84-2.23) |
| VM, halted and AP mailbox wait | 0.00 | 15.66 (15.45-15.77) | 3.01 (2.62-3.52) | 0.00 | 8.89 (8.66-9.03) | 1.98 (1.70-2.16) |
| VM, linker entries (own work) | 0.66 | 0.87 (0.85-0.89) | 0.26 (0.24-0.27) | 0.98 (0.96-1.00) | 1.54 (1.51-1.55) | 0.39 (0.37-0.41) |
| VM, dispatcher misses (own work) | 0.37 | 0.50 (0.50-0.51) | 0.12 (0.09-0.15) | 2.54 (2.52-2.56) | 3.44 (3.38-3.51) | 0.95 (0.91-0.98) |
| VM, entering blocks (`enter`, its copies) | 0.22 | 0.25 | 0.08 (0.07-0.09) | 0.76 (0.76-0.77) | 0.89 (0.88-0.89) | 0.24 (0.23-0.24) |
| VM, block lookup (`find`) | 0.11 | 0.14 | 0.04 | 0.35 | 0.50 (0.48-0.54) | 0.12 (0.11-0.13) |
| VM, link patching and placement | 0.19 | 0.27 | 0.08 | 0.35 (0.34-0.35) | 0.49 (0.48-0.50) | 0.13 (0.13-0.14) |
| VM, in-VM reuse (copies of kept templates) | 0.27 | 0.35 (0.34-0.36) | 0.08 (0.07-0.08) | 0.16 | 0.20 | 0.05 (0.04-0.06) |
| VM, system instructions | 0.13 | 0.14 | 0.09 | 0.07 | 0.11 | 0.03 |
| VM, CR3 loads and other TLB flushes | 0.24 | 0.14 | 0.03 | 0.19 | 0.27 (0.26-0.27) | 0.07 (0.06-0.08) |
| VM, shadow misses (abort + walk) | 0.29 (0.28-0.30) | 0.32 (0.28-0.35) | 0.12 (0.10-0.14) | 0.26 (0.25-0.27) | 0.40 (0.39-0.41) | 0.09 (0.08-0.10) |
| VM, revalidation and fetch walks | 0.14 | 0.19 | 0.05 | 0.22 | 0.29 (0.28-0.30) | 0.07 (0.07-0.08) |
| VM, deliveries (faults, interrupts) and IRQ polls | 0.09 (0.08-0.10) | 0.12 | 0.03 (0.02-0.04) | 1.57 (1.53-1.60) | 3.51 (3.37-3.60) | 0.90 (0.74-1.09) |
| VM, helpers, divisions, stops, L1 switches, shootdowns | 0.00 | 0.01 | 0.00 | 0.24 | 0.27 | 0.08 (0.07-0.08) |
| **Sum of the parts** | **7.73 (7.70-7.75)** | **25.53 (25.29-25.82)** | **6.38 (6.32-6.46)** | **19.88 (19.83-19.97)** | **36.87 (36.73-37.03)** | **9.22 (9.18-9.26)** |
| Window (wall START->END, or spawn->end point) x threads | 7.73 (7.70-7.75) | 25.53 (25.29-25.82) | 6.38 (6.32-6.46) | 19.88 (19.83-19.97) | 36.87 (36.73-37.03) | 9.22 (9.18-9.26) |

| Over the window (all threads), mean of 3 | Installer N = 1 | Installer N = 4 | `build` N = 1 | `build` N = 4 |
| --- | --- | --- | --- | --- |
| Block entries / linker entries / dispatcher misses, M | 4.79 / 2.62 / 2.11 | 5.03 / 2.78 / 2.17 | 18.75 / 2.88 / 15.78 | 19.05 / 3.12 / 15.84 |
| Host exits, k (HVF entry and exit 1.0 us each, bookkeeping 0.1 us) | 381 | 524 | 746 | 1,000 |
| System-instruction entries, k (largest kinds) | 1,279 (MOV from a segment register 323, STI run by the resident 314, IRET 205, INVLPG 80, SYSCALL and SYSRET 66 each) | 1,317 | 670 (STI 268, IRET 88, MOV from CR 83) | 688 |
| CR3 loads, k (K1 time per load) | 22 (10.6 us) | 2.6 (51 us) | 0.9 (200 us) | 0.9 (300 us) |
| Page faults delivered, k (K1 invalidation time) | 53 (0.05 s) | 52 (0.04 s) | 83 (**1.53 s**) | 84 (**3.45 s**) |
| Shadow-miss aborts / revalidation walks / in-VM reuse copies, k | 538 / 464 / 843 | 604 / 457 / 847 | 709 / 1,111 / 457 | 783 / 1,057 / 441 |
| Whole-cache flushes (metadata arena full), time | 4, 0.09 s | 4, 0.10 s | 5, 0.12 s | 4, 0.10 s |
| Translation requests, k | 328 | 475 | 752 | 1,004 |
| compiles (distinct bytes; repeats on the same vCPU; raced on another vCPU) | 126.5 (125.6; 0.8; 0) | 130.7 (128.6; 0.2; 2.0) | 152.0 (151.4; 0.6; 0) | 169.0 (152.9; 0.2; 15.9) |
| shared-cache hits (of them repeats after a whole-cache flush, one run) | 191 (98) | 334 (77) | 590 (497) | 825 (359) |
| Per compile, us: request / fexc round trip / fexc server CPU / pipe and wake-ups / admission / outside the parts | 19.8 / 16.1 / 12.6 / 3.5 / 1.25 / 1.8 | 22.4 / 18.3 / 14.5 / 3.8 / 1.30 / 2.0 | 19.5 / 16.0 / 12.5 / 3.5 / 1.16 / 1.7 | 23.9 / 20.1 / 15.3 / 4.8 / 1.17 / 1.9 |
| Per shared-cache hit, us: request / admission | 1.47 / 0.83 | 1.67 / 0.97 | 1.45 / 0.83 | 1.70 / 0.97 |

(System instructions, all kinds per workload, and the full tables with ranges: `tables.md`.)

**What the numbers say.**
1. *Installer, N = 1* (7.73 s logged): translation requests 2.80 s, of which fexc 2.04 s (126.5 k compiles, all but 0.8 k of
   bytes never compiled before, 16.1 us each: 12.6 us of server CPU and 3.5 us of pipe and wake-ups), admission 0.32 s and
   191 k shared-cache hits at 1.47 us; the rest outside the VM 0.69 s (381 k exits); generated code 1.48 s; the resident
   2.70 s spread over a dozen items of 0.1-0.66 s (linker entries 0.66 s, half of them system-instruction trips; shadow misses
   0.29; in-VM reuse 0.27; CR3 loads 0.24; misses 0.37). At N = 4 the four threads spend 15.7 of 25.5 s halted; vCPU 0's 6.38 s
   holds 1.46 s of requests and 3.0 s halted.
2. *`build`, N = 1* (19.88 s): generated code 7.08 s (36%); translation requests 3.83 s (fexc 2.43 s for 152 k compiles; 590 k
   shared-cache hits 0.86 s); the rest outside the VM 1.27 s; the resident 7.69 s, led by two items that are not inherent:
   **dispatcher misses 2.54 s** (15.8 M, plus their lookups and entries) and **page-fault invalidation 1.53 s** (83 k faults,
   18 us each against 0.8 us on the installer). At N = 4 (36.9 thread-seconds, 8.9 halted) the same two are 3.44 and 3.45
   thread-seconds; vCPU 0 has 0.95 and 0.90 s of them.
3. *Page faults.* A #PF (as an INVLPG) invalidates the blocks on its page, and, when its 2 MiB region's bit is set in the set
   of regions holding blocks fetched through 2 MiB pages, walks the linear map's chains of all 512 pages of the region
   (`cache::invlpg`). The set is 1,024 bits indexed by region number modulo 1,024, set by any such block (the kernel's text)
   and cleared only at a whole-cache flush. Scratch count, `build` N = 1, whole run: 5,840 such scans visited 69.7 M blocks
   (about 12 k each) and **2** of them met a block from a 2 MiB page; #PF invalidation 1.50 s. The installer: 117 scans.
4. *Dispatcher misses.* The dispatcher's L1 is 4,096 direct-mapped entries per code context, indexed by the RIP's low 12 bits.
   Scratch count at each miss, whole run: the entry held **another block** in 15.73 M of `build`'s 16.12 M misses (97.6%; empty
   0.35 M, this block 41 k) and in 1.70 M of the installer's 2.09 M (81%). In `build`, blocks placed, which bound a conflict with an
   earlier block for the same RIP, were 0.83 M: the rest are two hot RIPs at the same page offset. In 95.1% (`build`) and
   94.4% (installer) of these conflicts the two RIPs differ in bits 12-15, so a 65,536-entry table indexed by the low 16 bits
   would hold both (first order: conflicts new to the larger table are not counted).
5. *Whole-cache flushes.* Every flush is the 64 MiB metadata arena filling (as chunk 6 found); after it each block is asked
   for again and answered from the shared cache: 497 k of `build` N = 1's 590 k hits (5 flushes in the window), 98 k of the
   installer's 191 k.
6. *fexc.* The installer's stream through one server alone: 12.2-12.4 us of CPU per request (three replays). Busy samples
   (4,603 of 7,011 over 9 s of a six-fold replay): FEX's compile 39.3%; per-request setup 41.1% (argument parsing and string
   handling 24.9, allocation 6.3, the feature table 5.1, Config 3.5, JSON 1.3); the request function's own lines 6.6%
   (unclassified); reply write 8.0%; object packaging 4.1% (`profile/classify.txt`).
7. *Successors.* Of the shared-cache hits, 124 k (installer N = 1) and 494 k (`build` N = 1) were exits a predecessor's
   request listed on a page it fetched, with the answer already kept when that reply left; they were asked for a median
   2.7 us later. Most are the flush refills of item 5. Listed candidates never asked for: 175 k and 348 k.

**Candidates** (estimates, not measurements; arithmetic in `estimates.txt`). N = 4: thread-seconds divided by the cell's busy
(not halted) thread-seconds per wall second, 1.53 (installer) and 3.03 (`build`), an assumption that the saving is spread as the
busy time is. An exit outside a request is taken as 1.1 us (HVF 1.0 + bookkeeping 0.1, log off).

| Candidate: arithmetic | Installer N = 1 | Installer N = 4 | `build` N = 1 | `build` N = 4 |
| --- | --- | --- | --- | --- |
| (g) **Dispatcher L1 conflicts**: misses x conflict share x share a 64 Ki table separates x resident cost per miss (miss, its lookup and entry: 0.22-0.30 us); upper, first order | 0.38 | 0.33 | **3.22** | **1.40** |
| (g) **Page-fault region scans**: #PF invalidation time - faults x 0.8 us (the installer's cost per fault, 0.8-0.9 us) | 0.01 | 0 | **1.47** | **1.12** |
| (g) **Whole-cache flush refills**: repeats x (hit request + exit + placement, 2.7-3.0 us) + flush time; chunk 6 measured a 4x arena no faster on the installer (first touches) | 0.36 | 0.22 | 1.48 | 0.39 |
| (b) **fexc's per-request setup**: compiles x server CPU per compile x 41.1-47.7% | 0.66-0.76 | 0.51-0.59 | 0.78-0.91 | 0.35-0.41 |
| (c) **In-process FEX**: compiles x (round trip - server CPU + reply write 8% of server CPU) | 0.57 | 0.42 | 0.68 | 0.34 |
| (a) **Ahead-of-use placement**: answers compiled ahead in time (chunk 16's served counts) x 1.8 us, plus kept answers placed with the predecessor's reply x 1.5-1.6 us less the listed candidates never asked for x 0.24-1.3 us; most of the second part is the flush refills above | 0.07 + 0-0.15 | 0.04 + 0.03-0.20 | 0.12 + 0.31-0.68 | 0.04 + 0.18-0.34 |
| (g) System-instruction trips, all kinds: entries x about 0.2 us (chunk 17) | 0.26 | 0.17 | 0.13 | 0.05 |
| (h) CR3 loads (K1 time; `build`'s 200-300 us each walk the linked blocks) | 0.23 | 0.09 | 0.19 | 0.09 |
| (h) Raced compiles at N = 4: 15.9 k (`build`) x 20 us; chunk 13's in-flight table measured no wall gain | - | 0.03 | - | 0.10 |
| (d) Cheaper disk-cache key: with the cache off no key is computed (`ask` builds it only with a cache; 8 ns a request) | 0 | 0 | 0 | 0 |
| (e) Huge pages for the code area: the code area is already mapped with 2 MiB blocks (`memory::map_private` above the window's first 2 MiB); the second stage is HVF's | 0 | 0 | 0 | 0 |
| (f) Generated code: the whole bucket, a bound only (no experiment) | 1.48 | 1.28 | 7.08 | 2.89 |

Admission of shared-cache hits (0.16 / 0.32 / 0.49 / 0.80 thread-seconds) stays: every object is admitted at its RIP.

**Recommendation.** Next chunk: the two resident lookup costs that `build` shows, measured first and shipped only on the
usual rule. (1) The page-fault and INVLPG region test made exact (the regions that really hold blocks from 2 MiB pages, not a
1,024-bit hash cleared only at a flush): about 1.5 s of `build` N = 1 (7%) and 1.1 s at N = 4 (12%), nothing elsewhere. (2) A
larger dispatcher L1 (65,536 entries, the low 16 bits; 2 MiB of the code area per table, four tables, and a clear that costs
16 times more but happens 6-14 times a run): at most 3.2 s of `build` N = 1 and 1.4 s at N = 4, 0.3-0.4 s of the installer;
its first step is a scratch build with the larger table to measure the misses that remain. Both are small resident changes
with no new mechanism. For the installer's gap to QEMU (1.57 s at N = 1, 1.45 s at N = 4), the next after that is fexc's
per-request setup with in-process FEX (together about 1.2-1.3 s at N = 1, 0.9-1.0 s at N = 4), then the flush refills (0.36 s;
the arena-size result of chunk 6 says to measure the first-touch cost first). Ahead-of-use placement is not worth its own
chunk on these numbers (at most 0.2 s on the installer), and generated code has no estimate without an experiment.

**Not done.** No optimisation built; no instrumentation kept (the four scratch diffs are in the evidence directory; the
tree is HEAD's). Not measured: the share of HVF's second-stage TLB misses in generated code (no counter reachable).

### Measured (chunk 19: exact large-page regions and a larger dispatcher L1, November 4; uncommitted on f74c3ea, series55-swapgs)

Evidence: `natemu-raw/lookup-20261104-01/` (README there). Setup as chunk 18: the `natemu` stub, cache off,
`mitigations=off nokaslr`, 2 GiB, compiler series55-swapgs (unchanged), one VM at a time. Walls are uninstrumented runs
(default resident, no log), interleaved; counts come from single runs with the perf resident and its log (whole run, the
log's final per-vCPU lines). The two fixes were built and measured one after the other: fix 1 against HEAD, then fix 2 on
top of fix 1 against fix 1 alone.

**Fix 1: the page-fault and INVLPG region test made exact.**
*Measured first* (scratch counters, `scratch-s1count.diff`, not kept; one run each, whole run): of `build` N = 1's 85.0 k
page faults, 4,386 found their 2 MiB region's bit set in the 1,024-bit set; with the exact test below 129 of them scan the
region, and the same 2 scans as before meet a block from a 2 MiB page. At most 8 regions were recorded at once. The
installer N = 1: 276 bit hits, 2 scans. `build` N = 4, vCPU 0: 1,009 and 186. Page-fault time of `build` N = 1 fell from
1.60 s (chunk 18's scratch run of HEAD) to 0.13 s.

*Built* (resident `cache.rs`; abi `Control`). Besides the 1,024-bit set, the cache now keeps the whole numbers of the
regions holding a block fetched through a 2 MiB leaf, up to 64 (`Control::large_list`, `large_count`), recorded
where the bit is set (placement and a revalidation that finds a large leaf, both regions of the block's first and last byte)
and cleared only at a whole-cache reset. An INVLPG or #PF scans a region's 512 chains when its bit is set and the region is
listed; past 64 regions the list is incomplete and the bit alone decides, as before. A record is never removed while its
blocks live, so it can only cause a scan, never miss one: the set of blocks cut is the same as chunk 8's (SDM Vol.3
4.10.4.1). Every scan the list removes was at a region whose bit another region had set (a bit is set only where its region is
listed, and the list never filled): region numbers alias modulo 1,024.

*Tests* (`tools/run_k1.py`, natemu only, 1-4 vCPUs, both block sizes, 48 runs green): `pf-large` (a #PF on another 4 KiB
page of a 2 MiB code page, the PDE remapped by the handler with no INVLPG: the code runs from the new frame), `large-alias`
(a large block 2 GiB above, in the same bit, placed first: an INVLPG of another page of the region still cuts the region's
own block), `large-reval` (a block placed through 4 KiB pages, then revalidated through a 2 MiB page of the same frames
after a CR3 reload, then cut by an INVLPG elsewhere in the 2 MiB), `large-64` and `large-65` (large blocks in exactly as
many regions as the list holds, and one more: an INVLPG in the last region cuts its block). Each red under a scratch mutation (`nc/`, outputs kept): the region test always false (`invlpg-large`,
`pf-large` r9 1), a region not listed when its bit was already set (`large-alias` r9 1), a revalidation that does not record
(`large-reval` r9 1), the list taken as complete past 64 (`large-65` r9 1), the list's last entry not looked at
(`large-64` r9 1), the overflow threshold raised by two (`large-65`: r9 0 and resident stops, status 4).
A list one slot shorter or a threshold one higher stays correct (each block records two bytes, so past a full list the
count rises by two) and was not counted as a control.

| Wall s, mean (min-max) | HEAD | fix 1 | change |
| --- | --- | --- | --- |
| `build` N = 1 | 19.67 (19.37-19.90) | 18.22 (18.07-18.35) | -1.45 (-7.4%) |
| `build` N = 4 | 9.06 (8.92-9.22) | 8.38 (8.34-8.44) | -0.67 (-7.4%) |
| `hackbench` N = 1 (6 runs each) | 13.55 (13.09-13.77) | 13.76 (13.57-13.87) | +0.21 |
| `hackbench` N = 4 (6 runs each) | 4.41 (4.04-4.77) | 4.46 (4.18-4.72) | +0.05 |
| `locks` N = 1 / N = 4 | 5.47 / 6.56 | 5.52 / 6.57 | +0.05 / +0.02 |
| Installer N = 1 (6 runs each) | 7.60 (7.46-7.73) | 7.66 (7.50-7.79) | +0.05 |
| Installer N = 4 (one run each) | 6.04 | 6.20 | +0.16 |

(`camp1/`.) The other cells move within their spreads. `hackbench` N = 1, two perf runs each (`camp1/perf/`): page-fault
time 15 ms and INVLPG time 21 ms in both builds, logged walls 14.25-14.35 s in both: the fix does not reach it.

**Fix 2: a 65,536-entry dispatcher L1.**
*Measured first* (scratch builds of the table at 16 and 14 index bits with nothing else changed, `scratch-l1s16.diff`; a CR3
load there still scanned every entry; one perf run each): dispatcher misses at 4,096 / 16,384 / 65,536 entries, `build`
N = 1 16.11 / 6.30 / 2.10 M (resident miss time 2.60 / 1.10 / 0.46 s), the installer N = 1 2.11 / 1.31 / 0.63 M. The full
scan of the larger tables at each CR3 load cost 2.25 s (`build`) and 2.62 s (installer) against 0.33 and 0.24 s: a CR3 load
must not visit every entry.

*Built* (abi cache layout, resident `cache.rs`, `gates.S`):
- L1 is indexed by the RIP's low 16 bits (`L1_BITS`): four tables of 2 MiB in the code area's head (8.07 MiB, from 0.57),
  the owners 1 MiB of the lookup area. The tables lie beyond the reach of `adr` and `cbnz`, so the dispatcher names a
  table with `adrp` (the tables are page aligned; the resident writes the dispatcher's own `adrp` at fill time, and checks
  every slot's for every table, as it checked the `adr`), and an entry is `movz/movk x2, #rip; sub; cbnz x2, +8; b entry;
  b miss` (a hit runs the same instructions as before; a miss one more `b`).
- CR3 loads through fill lists. A CR3 load moves `epoch` and not `global_epoch`, so every global block stays current: only
  the entries filled with a non-global block can need emptying. Each table keeps the indices of those entries
  (`L1_LISTS`, 1 MiB; an owner bit marks an index as listed, so it is listed once); a CR3 load visits that list, empties
  the stale entries (each cleaned to the point of unification, one barrier, then its instructions invalidated) and keeps
  only the entries still filled. A global flush, a reset and a table taken by another context clear the whole table as
  before.
- Not changed: the miss path, `fill_l1`'s rule (a block enters only its own context's table), the invalidation of one
  block's entry in every table. The vCPU windows stay inside the 1 GiB identity map (the compile-time check: 650 of
  1,024 MiB). Linux runs give blocks 120 MiB of code area instead of 127.5 MiB (the peak used: 38 MiB on `build`).

*Costs* (one perf run each, whole run). A table clear takes about 0.24 ms: the clears of a table taken by a new context, 11
on `build` N = 1 and 13 on the installer, took 2.6 and 2.9 ms (0.16 and 0.19 ms before); CR3 loads and global flushes together 0.21 s on `build` N = 1 and 0.08 s on the
installer, against 0.33 and 0.24 s with the 4,096-entry tables scanned whole. Max RSS +6 to +11 MiB at N = 1 and +19 to
+29 MiB at N = 4. Misses: `build` N = 1 2.13 M (16.11 M), installer 0.63 M (2.11 M); resident block entries 6.00 M and
3.36 M (19.95 M and 4.80 M); generated code 6.51 s and 1.42 s (7.68 and 1.53 s).

*Tests* (`run_k1.py contexts`, extended): two RIPs with the same low 16 bits called in turn three times under one
context each run their own code, then the large page under them is remapped under another context with a CR3 reload and
no INVLPG, and the second RIP called under the first context runs the new code (the reload reached another context's
table). Green at 1-4 vCPUs and both block sizes. Red under scratch mutations (`nc/`): the CR3 load looking at the current
table only (`contexts` r12, rdi), entries never listed and only global ones listed (`remap-cr3`, `cr3-switch`), the entry's
RIP comparison removed (`contexts` rsi 3 for 0x30); the listed bit left set where an index leaves its list at a CR3 load
(`cr3-switch` r8 0x63 for 0x32, also `remap-cr3`, `dormant-write`, `mixed-g0`, `mixed-g1`) and at a table clear
(`contexts` r12 3 for 4).

| Wall s, mean (min-max) | fix 1 | fix 1 + 2 | change |
| --- | --- | --- | --- |
| `build` N = 1 | 18.26 (17.97-18.42) | 14.39 (14.19-14.53) | -3.87 (-21.2%) |
| `build` N = 4 | 8.28 (8.06-8.59) | 6.96 (6.82-7.14) | -1.31 (-15.8%) |
| `hackbench` N = 1 | 13.87 (13.47-14.17) | 13.44 (13.35-13.49) | -0.43 (-3.1%) |
| `hackbench` N = 4 | 4.42 (4.33-4.62) | 3.93 (3.80-4.11) | -0.50 (-11.2%) |
| Installer N = 1 (6 runs each) | 7.71 (7.45-7.88) | 7.13 (7.04-7.26) | -0.58 (-7.6%) |
| Installer N = 4 (one run each) | 6.15 | 5.92 | -0.23 |
| `locks` N = 1 | 5.58 (5.53-5.64) | 5.63 (5.61-5.65) | +0.04 |
| **`locks` N = 4** | 6.53 (6.50-6.57) | **7.00 (6.55-7.26)** | **+0.47 (+7.2%)** |

(`camp2/`.) Against HEAD, across the two campaigns (`camp1`'s HEAD column): `build` N = 1 19.67 to 14.39 s, N = 4 9.06 to
6.96 s, the installer N = 1 7.60 to 7.13 s.

*Size* (`size/`, three interleaved rounds of the four builds): wall s, mean.

| | 4,096 (fix 1) | 16,384 | 32,768 | 65,536 |
| --- | --- | --- | --- | --- |
| `build` N = 1 | 18.53 | 15.73 | 14.89 | 14.61 |
| `hackbench` N = 4 | 4.38 | 3.95 | 4.08 | 4.15 (3.80-4.39) |
| Installer N = 1 | 7.79 | 7.38 | 7.35 | 7.32 |
| `locks` N = 4 | 6.48 | 6.93 | 7.03 | 7.10 |

65,536 entries kept: `build` gains 1.1 s more than at 16,384; the others do not separate beyond their spreads.

*`locks` N = 4 is slower at every larger size: shipped as a measured exception* (the coordinator's decision after the
independent review; the cell is contention-sensitive, chunk 15). The same build at 4,096 entries (fill lists and the new entry form, the
table size alone unchanged: `c19b12`) ran 6.60, 6.65, 6.56 s against fix 1's 6.63, 6.59, 6.30 and 64 Ki's 7.22, 7.12, 7.18
(`size/runs12/`): the cost comes with the index, not with the lists or the entry form. Three perf runs each (`locks/`,
`locksum.py`): per resident entry the costs are the same (generated code 0.230 against 0.231 us per entry, linker entries
0.089 against 0.089 us), but the guest does more: SYSCALL and SYSRET 23.2 M each against 25.4 M (+9%), block entries
+5% and linker entries +9%, halts 6 k against 35 k, dispatcher misses 7.8 M against 6.6 M. The benchmark is four threads on
one mutex (futex wait and wake). What triggers the extra SYSCALLs and halts was not identified.

**Checks** (`suite19.sh`, `suite/`, the final tree, one VM runner at a time, each case's output deleted as it ended):
`cargo test` 106 + 1 ignored (host), abi 65, also with `FEXC` set; the resident stack audit `ok`; the Python unit tests 317
(the first pass failed the stack audit's own test, whose mutation looked for the dispatcher's `and x1, x0, #0xfff`; now
`#0xffff`, rerun green); fixtures 64 and seed 44 at both block sizes; the i7 corpus at CPL 0 69 at both; faults 7,912 at
both; `run_k1.py` 328 runs, no problem (the new cases and the extended `contexts` included); `run_k2.py` all ok (`swapgs`
included); `run_slice3_vm.py` `system`, `delivery`, `events`, `contain`, `smp` 0 failures; `run_x3.py` 54/54 and every
group, `run_x4.py` 33/28/6, `run_l4a.py`, `run_l4b.py`, `run_s4.py` all ok; `run_boot_smoke.py` 4/4; `run_r1.py r1a` 7/7,
`r1b` 12/12 (negative controls flagged); FEX ASM 2,270: 1,898 pass, 48 precision deviations, 324 unsupported, no failure.
`--check-reuse --code-shape` installer boots, cache off (`checkboot19.py`, `check/`): N = 1 856,622 copies checked, N = 4
871,772 over four vCPUs, no halt, both reached the dialog. Every measurement run verified or reached the dialog. After the
independent review (`suite/r1/`: comment changes, the dispatcher template's unused `adrp` offset dropped, `large-many`
replaced by `large-64` and `large-65`, five more controls): `cargo test` with and without `FEXC`, the stack audit and its
test, `run_k1.py` 336 runs, all green.

**Not done.** The `locks` N = 4 exception is not resolved (above). Blocks from 1 GiB pages: not reachable (the walker
treats PS at level 3 as reserved and the profile does not advertise 1 GiB pages); revisit if they are added (the region test
covers 2 MiB regions only).

### Measured (chunk 20: fexc's per-request setup, November 4; uncommitted on 5d80597, series56-setup)

Evidence: `natemu-raw/fexsetup-20261104-01/` (README there). Setup as chunk 18: the `natemu` stub at HEAD 5d80597, cache
off, `mitigations=off nokaslr`, 2 GiB, one VM at a time. Old compiler series55-swapgs, new series56-setup. Host and
resident unchanged.

**Profile first.** An installer N = 1 boot's compile stream (125,799 requests) and a `build` N = 1 run's (229,676), recorded
by a `tee` in front of the server, as in chunk 18. Each holds 6 distinct argument lists once the RIP is left out (profile,
12 feature overrides, gating arguments, an occasional `--max-insts 1`). Through one series55-swapgs server alone the
installer stream cost 12.4 us of server CPU per request (12.38-12.57, three replays) and `build`'s 12.2 (12.13-12.28).
`sample` of the server replaying the installer stream six times (`profile/old.*`): of the busy samples, FEX's compile
40.9%, argument parsing and string handling 22.0% (each argument copied and compared with every option name; each of the
12 overrides compared by name with every one of the 33 features), allocation 6.9%, the feature table and its 66
`std::function`s rebuilt 4.7%, Config torn down and rebuilt 3.7%, JSON 1.1%, the reply write 7.7%, the request function's
own lines 7.7%, object packaging 4.0%. This confirms chunk 18's breakdown.

**Built** (`tools/fexc/fexc.cpp`; no FEX patch). The request is split into `ParseArgs` (arguments into a `Setup`),
`CheckWindow` (the input size and RIP window), `Configure` (Config values, host features, the kept context) and
`CompileRequest` (the compile and the reply). The server keeps one parsed and configured `Setup` per distinct argument list
less the `--rip` value. The key is the argument bytes before and after the value of the first `--rip` token, with that
value's offset. A list is kept only when that token is the RIP the parse used, so a list with two `--rip` (the last
applies) or with `--rip` as another option's value is parsed every time, as before. A request whose list matches a kept
one compiles with the kept setup at its own RIP: the parse would have consumed the same tokens in the same order, none of
its choices depends on the RIP's value, and the window check, the one check that reads the RIP, runs for every request.
What is kept, and what invalidates it:
- *Per argument list (never invalidated; a different list is a different entry):* every option's value (profile, block
  limit, bitness, TSO, multiblock, PC-table density, end marker, the gating arguments `--sse-gated` ... `--x87-unmasked`,
  `--cpl0`, `--tsc`, feature overrides, `--config`), the Config values derived from them, the host features and the kept
  context (already one per configuration since P3).
- *Per request:* the RIP, the guest bytes and the window check; `ResetGlobals`; then, once (`Prepare`, before a new setup's
  context is created), the guest window, the shim's gating arguments, hooks, PC-table and profile flags and the log handlers
  set from the setup, and Config, so a request never sees another list's flags.
- *Config:* rebuilt from nothing (`Shutdown`, `Initialize`, the values) only when a request's values differ from the last
  ones applied. Nothing in FEX changes a value after it is built (`Config::Set` is called only by fexc; the reader only caches its
  conversion of a stored string), so the values left from
  the previous request are the same values.
- *Feature table:* built once, with plain function pointers; a request's overrides go to a copy of the defaults (only when
  a list is first configured).
- *Reply:* one `writev` of the header, JSON and object from their own buffers; the object is built in the reply, whose
  buffers, like the request buffer, the server reuses.
The one-shot CLI runs the same four steps once; `--serve --report` and `--serve-mode fork` are unchanged (in fork mode the
child parses and configures; nothing is kept in the server). `test_serve.py` gains eight cases for the key's edges: two
`--rip` with either value differing, `--rip` as an option's value, a kept list at other RIPs and at a RIP outside the
address space. Compiler **series56-setup** (`obj/fexc` SHA-256 `d3222fda...7db064`, reproduced by a second `build.sh`),
measured below; after the independent review (no blocking defect) **series56-setup-r1** (`a81a2f24...add1fd8`): the guest
window set before a new setup's context is created (one `Prepare` per request; the Config values derived when the
arguments are parsed), the reused reply's extent and args_read cleared at each request's start, comments; handoff §6.

**Identity (blocking gate).** Both recorded streams replayed in order, in lockstep, through series55-swapgs and
series56-setup servers run as the host runs them (`replay20.py`): **every reply byte-identical**, so 0 differences in code,
object, extent, args_read, status and `guest.end` (installer: 125,105 objects, 694 without code; `build`: 228,773 and
903). The installer stream also with `--report` on both (the full report) and with `--serve-mode fork` on both: 0
differences. `test_serve.py` (now 1,978 requests: fixtures, seed corpus, memory-form matrix, adversarial orders, the new
cases) identical in reuse and fork mode and against series55-swapgs's CLI. Negative controls (`nc/`), each red in
`test_serve.py`: a hit compiling at the kept list's RIP (3,028 differences), keeping a list whose first `--rip` is not the
one used (6, the two-`--rip` cases), not setting the gating arguments on a hit (2,843). `--check-reuse --code-shape`
installer boots with series56-setup, cache off: N = 1 852,252 resident copies checked, N = 4 871,499 over four vCPUs, no
mismatch, both reached the dialog.

**Server CPU** (each stream alone through one server, three replays each): installer 12.47 us per request (12.38-12.57) to
**8.08** (8.01-8.15), `build` 12.23 (12.13-12.28) to **7.92** (7.91-7.94): -4.4 us (-35%). Profile of the new server
(`profile/new.*`, busy samples): FEX's compile 68.6%, the reply write (the `writev` system call itself) 13.8%, the request
functions' own lines 7.1%, `ResetGlobals` (freeing the previous request's recorder containers) 4.2%, allocation 2.2%,
arguments and strings 1.8% (the key and its lookup), feature table, Config, JSON and packaging 0.4-0.5% each.

**Measured** (wall s, uninstrumented; `camp/summary.md`; three interleaved rounds, old first in each; installer N = 4 once;
`build`, `hackbench`, `locks`, `zstd` from START to END, boot excluded; CPU = the process's user + system, its reaped
servers included; every run verified or reached the dialog, none contended):

| Workload | N | wall s, series55-swapgs | wall s, series56-setup | change of means | CPU s, old | CPU s, new |
| --- | --- | --- | --- | --- | --- | --- |
| installer (spawn to the dialog) | 1 | 7.16 (7.05-7.27) | **6.53** (6.44-6.65) | -0.63 s (-8.8%) | 7.18 | 6.54 |
| installer | 4 | 6.00 (one run) | **5.51** (one run) | -0.49 s (-8.2%) | 9.49 | 8.54 |
| `build` | 1 | 14.37 (14.26-14.45) | **13.63** (13.52-13.71) | -0.74 s (-5.1%) | 17.31 | 16.21 |
| `build` | 4 | 6.91 (6.76-7.18) | **6.71** (6.61-6.82) | -0.20 s (-2.9%) | 23.29 | 22.05 |
| `hackbench` | 1 | 13.47 (13.40-13.53) | 13.48 (13.44-13.52) | +0.01 s | 16.37 | 15.98 |
| `hackbench` | 4 | 3.87 (3.83-3.89) | 3.83 (3.78-3.89) | -0.04 s | 18.89 | 18.44 |
| `locks` | 1 | 5.65 (5.64-5.66) | 5.69 (5.67-5.71) | +0.04 s | 8.54 | 8.17 |
| `locks` | 4 | 6.94 (6.89-7.02) | 7.03 (6.98-7.11) | +0.08 s | 31.01 | 31.06 |
| `zstd` | 1 | 2.05 (2.04-2.06) | **1.98** (1.95-2.00) | -0.07 s (-3.5%) | 4.99 | 4.54 |
| `zstd` | 4 | 1.17 (1.14-1.20) | **1.11** (1.08-1.13) | -0.06 s (-5.1%) | 6.39 | 5.90 |

Every benchmark's boot to its START line is 0.37-0.41 s shorter (N = 1 2.87-2.88 to 2.48-2.50 s; N = 4 2.93-2.97 to
2.53-2.57). `locks` with the order reversed (three more rounds, new first; `camp-extra/`): N = 1 5.68 (5.55-5.76) old
against 5.56 (5.43-5.65) new, N = 4 7.08 (7.00-7.18) against 7.05 (6.80-7.25): the sign follows the order, so `locks` is
unchanged; `hackbench` is unchanged too. The installer N = 1 and `build` N = 1 savings (0.63 and 0.74 s) are 0.03-0.16 s
under chunk 18's estimates (0.66-0.76 and 0.78-0.91), N = 4 installer at its lower end (0.49 against 0.51-0.59) and `build`
N = 4 under it (0.20 against 0.35-0.41). **Ship threshold met**: installer, `build` and `zstd` faster beyond the spread at
both sizes, nothing slower.

**What in-process FEX would add** (estimate on these numbers, not built: it changes the isolation posture and needs an owner
decision): per compile, the pipe and wake-ups (round trip less server CPU, 3.5 us in chunk 18) plus the reply write (13.8%
of 8.1 us, 1.1 us), about 4.6 us: about 0.58 s on the installer N = 1 (126.5 k compiles) and 0.70 s on `build` N = 1
(152 k).

**Checks** (`suite20.sh`, `suite/`, the final tree with series56-setup, one VM runner at a time, each case's output deleted
as it ended): `cargo test` 106 + 1 ignored (host), abi 65, also with `FEXC` set;
the resident stack audit `ok`; the Python unit tests 317; fixtures 64 and seed 44 at both block sizes; the i7 corpus at CPL 0
69 at both; faults 7,912 at both; `run_k1.py` 336 runs, no problem; `run_k2.py` all ok, and `run_k2.py --only swapgs` (its
`msr` at both block sizes, `fault`, `kicks`, `events`, `contain`) all ok; `run_slice3_vm.py` `system`, `delivery`, `events`,
`contain`, `smp` 0 failures; `run_x3.py` 54/54 and every group, `run_x4.py` 33/28/6, `run_l4a.py`, `run_l4b.py`,
`run_s4.py` all ok; `run_boot_smoke.py` 4/4; `run_r1.py r1a` 7/7, `r1b` 12/12 (negative controls flagged); FEX ASM 2,270:
1,898 pass, 48 precision deviations, 324 unsupported, no failure. The compiler's own checks (`checks/`): `test_fexc.py` 10,
`test_slice1.py` 37, `check_sites.py --slice1` 64 programs (779 sites, 0 errors), `verify_precision.py` PASS,
`test_serve.py` above. Rerun on series56-setup-r1 after the review (`review/`): both recorded streams as the host runs the
server and the installer's with `--report`, 0 differences against series55-swapgs; `test_serve.py` identical in reuse and
fork mode; `test_fexc.py` 10; `cargo test` with `FEXC` 106 + 1 ignored, abi 65. Replies byte-identical, so the walls were
not measured again. The recorded streams were deleted afterwards (0.83 GB; `rec/fexc` records them again).

**Not done.** The shim's recorder containers are still freed and refilled per request (`ResetGlobals`, 4% of the new
server's busy time), and the host still formats the argument strings for every request (host side, unchanged).

### Measured (chunk 21: a shared-memory channel to the compile server, November 4; uncommitted on af2acf6, series58-channel-r1)

Evidence: `natemu-raw/shmchan-20261104-01/` (README there). Setup as chunk 18: the `natemu` stub, cache off,
`mitigations=off nokaslr`, 2 GiB, one VM at a time. Old: HEAD af2acf6 with compiler series57-fork over the pipe. New: the
working tree with **series58-channel** (`obj/fexc` SHA-256 `da337021...87315c`, reproduced by a second `build.sh`), measured;
after the independent review **series58-channel-r1** (`8bd3b7ea...a83e59`, reproduced likewise; below, "Review"). fexc stays a
separate process; no FEX change.

**Breakdown first.** In the VM, at HEAD (perf resident and log, two installer and two `build` N = 1 runs): a compile's round
trip took 11.24-11.40 us, of which the server's CPU (wait4) 7.80-7.84 us and the rest, pipe and wake-ups, 3.44-3.56 us. After
a reply the vCPU asked for its next compile a median 7.5-7.6 us later (85-86% within 10 us, 94-95% within 20 us); 99.6% of
round trips took at most 30 us. The parts, from a two-process model of the transport (`transport/chan.c`: fixed-work loops,
a compile of 8 us of CPU and the installer's measured gaps replayed, 100,000 requests, two rounds, nothing else running), us
per request:

| Transport, N = 1 | Round trip less the server's work | Host's send | To the server holding the request (its wake, its reads) | Server's reply send to the host's wake (the send call itself) | Host's receive |
| --- | --- | --- | --- | --- | --- |
| Pipe (today's calls: two writes; two reads; one `writev`; poll, read, poll, read) | 4.37 / 4.08 | 0.70 / 0.67 | 1.25 / 1.16 | 1.61 / 1.48 (0.58 / 0.53) | 0.82 / 0.78 |
| Shared mapping, both sides sleep at once (`os_sync_wait_on_address`) | 3.52 / 3.42 | 0.63 / 0.61 | 1.13 / 1.10 | 1.72 / 1.68 (0.62 / 0.59) | 0.04 |
| Shared mapping, only the host spins (20 us) | 5.00 / 4.89 | 0.63 / 0.61 | 4.26 / 4.18 | 0.08 | 0.03 |
| Shared mapping, only the server spins (20 us) | 4.82 / 4.79 | 0.08 | 0.13 / 0.12 | 4.57 / 4.54 | 0.04 |
| Shared mapping, both spin (host 20 us, server 20 us) | 0.44 / 0.43 | 0.08 | 0.25 | 0.08 | 0.02 |

Without spinning the channel saves about 0.9 us of the 4.1-4.4 (about 0.11 s of the installer N = 1, under the 3% bar); a
spin on one side makes the other side's wake-up slower (4.2-4.6 us) than the pipe's; both spinning removes almost all of it,
at about 7 us more CPU per request on each side (host 24.0-24.4 against 17.1-17.3 us per request with the gap's work, server
17.3-17.6 against 10.0-10.3). Four pairs at once (N = 4): pipe 11.0 us, both sleeping 9.3, both spinning 0.9.

**Built.** `fexc --serve --channel FD` (`tools/fexc/fexc.cpp`): the same request and reply payloads through a 16 MiB mapping
(request word and length at 0 and 4, reply word and length at 64 and 68, request payload at 4 KiB, at most 1 MiB, reply
payload after it, at most 15 MiB less 4 KiB; a larger reply is an error reply). Request n is posted by writing its payload and
length, then n << 1 into the request word; the reply likewise into the reply word. Bit 0 of a word says its reader sleeps; the
poster wakes it (`os_sync_wake_by_address_any`) only then. The server spins 20 us for the next request, then sleeps in 100 ms
slices and leaves when stdin reaches EOF; an empty request ends it. Fork mode, `--report` and the pipe protocol are unchanged
(every mode works with either transport); the one-shot CLI is unchanged. The layout carries a magic word ("CHN1"; the host
writes it at 8, the server checks it at start and writes it at 72, the host checks that with every reply). Host (`crates/host/src/execution.rs`, `Server`,
`Channel`): the mapping is `shm_open`ed (its name unlinked at once), sized and mapped per server, and inherited by the server
as an open fd, close-on-exec except during that spawn (under the existing spawn lock). The host must never fork while a VM runs:
the spawn takes no `pre_exec`, `uid` or `gid`, which would make std fork instead of `posix_spawn` (see the defect below). The host spins 30 us for the reply, then
sleeps in 5 ms slices until the request's deadline; after each slice it polls the server's stdout, which carries nothing in
channel mode, so EOF means the server died (`UnexpectedEof`, as the pipe gave) and any byte is refused. The reply length is
bounds-checked (20 bytes to the reply area's size) and the payload copied out of the mapping before it is parsed by the same
checks as before; nothing the host trusts lives in the mapping. Deadline expiry still kills the server (`TimedOut`), and
`finish` posts the empty request before closing stdin and reaping it.

*Spin bounds* (scratch builds with the bounds set from the environment, installer N = 1 and 4, `build` N = 1 and 4,
interleaved; `var/`, `var2/`): neither side spinning: no wall change (installer N = 1 6.43 against 6.48 s); one side only:
no gain and more CPU (host 30 us only 6.32 s, CPU 7.35 against 6.51 s; server 20 us only 6.41 s, 7.71); both: 5.27-5.63 s at
N = 1 and 4.58-4.70 s at N = 4 for every pair tried (host 30 or 100 us, server 10, 20 or 50 us), within each other's spread.
Kept: host 30 us, server 20 us. These scratch builds, like the first campaign below, spawned the server through `pre_exec`:
their walls against `base` (HEAD) carry that fork's cost, so only the variant-against-variant comparisons are fair.

*A defect found by the suite and fixed.* The first build passed the fd as fd 3 through `Command::pre_exec`, which makes std fork
instead of `posix_spawn`. With it `run_x3.py` stress (`apic-stress-x2@4`) and `run_l4a.py` stress (`l4a-stress-x@4`) failed,
twice: a vCPU stopped receiving interrupts and its peers' watchdogs fired. The guest RAM is a private anonymous mapping, which a
fork makes copy-on-write in the parent while the VM runs. With the fd inherited instead both pass (6/6 and 2/2) and the suite
below is clean. Every measurement in the tables below is of the fixed build; the replays and walls of the first build are kept
in `camp-fork*/`, `final-fork/`.

**Identity (blocking gate).** An installer N = 1 boot's compile stream (125,882 requests) and a `build` N = 1 run's (229,774),
recorded at HEAD by a `tee` in front of the server, replayed in lockstep through series57-fork over the pipe and
series58-channel over the channel driven by the host's own `Server` (natemu's ignored test `channel_replay_matches_the_pipe`):
**every reply byte-identical** (code, JSON, object, extent, args_read); also the installer stream with `--report` and with
`--serve-mode fork` on both: 0 differences. Over the pipe on both sides (`replay20.py`, series57-fork against
series58-channel): 0 differences in all four. `test_serve.py` (1,978 requests) identical in reuse and fork mode and against
series57-fork's CLI.

**Robustness.** New host tests with a stand-in server (`fake_server`, a Python script on the mapping):
`channel_refuses_malformed_replies` (a length past the reply area, 0xffffffff, under the 20-byte header, a header whose JSON
and object lengths do not add up, one with a wrapping JSON length, a byte on stdout: each refused, and a well-formed reply
after a refused one read as usual) and `channel_detects_a_dead_or_hung_server` (a server that kills itself holding the
request: `UnexpectedEof` in under 2 s against a 30 s deadline, its exit named `signal: 9`; one that never answers: `TimedOut`
at the 300 ms deadline). In the VM, `run_slice3_vm.py contain`'s cases kill a vCPU's server before its 51st request and stop
another with SIGSTOP: the runs fail at once naming the server, and at the compile deadline, as before. Negative controls
(`nc/`), each red: the length bound removed (`channel_refuses_malformed_replies` fails), the death check removed (both tests
fail, the killed server's request waiting 30 s for its deadline), the deadline ignored (the hung test never ends; stopped at
180 s), the reply copied one byte short (replay: 77,341 of 125,882 replies differ), the server copying its JSON one byte short
(replay: 125,880 differ).

**Per compile in the VM** (perf resident and log, installer and `build` N = 1, one run each of old and new taken one after the
other; `final/analysis.jsonl`): round trip **13.07 to 5.46 us** (installer) and **13.53 to 5.18 us** (`build`); the server's CPU
per compile 9.08 to 12.89 and 9.30 to 12.35 us, of which spinning for the next request is about 7.6 and 7.3 us (the mean of
the gap to the next request, capped at 20 us), so its compile CPU fell to about 5.3 and 5.1 us; the vCPU threads' CPU 6.10 to
6.19 s and 15.90 to 15.55 s. The round trip is now below the old server CPU per compile: what fell is not only the transport.
(The HEAD runs of the breakdown, earlier in the day, measured 11.24-11.40 us round trip and 7.80-7.84 us server CPU.)

**Measured** (wall s, uninstrumented; `camp/summary.md`; three interleaved rounds, old first in each; installer N = 4 once;
`build`, `hackbench`, `locks`, `zstd` from START to END, boot excluded; CPU = the process's user + system, its reaped
servers included; every run verified or reached the dialog, none contended):

| Workload | N | wall s, old | wall s, new | change of means | CPU s, old | CPU s, new |
| --- | --- | --- | --- | --- | --- | --- |
| installer (spawn to the dialog) | 1 | 7.05 (6.99-7.17) | **5.85** (5.71-6.13) | -1.20 s (-17.0%) | 7.08 | 7.28 |
| installer | 4 | 5.84 (one run) | **4.78** (one run) | -1.06 s (-18.2%) | 9.20 | 9.38 |
| `build` | 1 | 14.47 (14.38-14.52) | **12.81** (12.79-12.84) | -1.66 s (-11.5%) | 17.32 | 17.48 |
| `build` | 4 | 6.64 (6.52-6.71) | **5.57** (5.40-5.66) | -1.07 s (-16.1%) | 22.78 | 23.31 |
| `hackbench` | 1 | 14.00 (13.98-14.02) | 13.92 (13.86-13.95) | -0.08 s | 16.75 | 16.83 |
| `hackbench` | 4 | 3.90 (3.77-3.98) | 3.82 (3.77-3.92) | -0.08 s | 18.84 | 18.83 |
| `locks` | 1 | 5.83 (5.79-5.86) | 5.73 (5.71-5.77) | -0.10 s | 8.61 | 8.62 |
| `locks` | 4 | 6.99 (6.88-7.21) | 6.82 (6.54-7.01) | -0.17 s | 30.70 | 30.73 |
| `zstd` | 1 | 2.03 (1.98-2.08) | **1.94** (1.91-1.96) | -0.09 s (-4.4%) | 4.87 | 4.92 |
| `zstd` | 4 | 1.18 (1.15-1.20) | **1.06** (1.04-1.09) | -0.12 s (-10.2%) | 6.20 | 6.37 |

Every benchmark's boot to its START line is 0.73-0.80 s shorter (N = 1 2.72-2.74 to 1.92-1.94 s; N = 4 2.78-2.81 to
2.03-2.08). With the order reversed (three rounds new first, `camp-extra/`): `hackbench` N = 4 4.14 (4.03-4.22) old against
3.95 (3.78-4.16) new, `locks` N = 4 7.16 (6.78-7.44) against 6.79 (6.49-6.96), `zstd` N = 4 1.16 (1.12-1.19) against 1.09
(1.04-1.14); `hackbench` N = 4 four more rounds alternating (`camp-extra2/`): 3.88 (3.83-3.98) against 3.96 (3.81-4.08), so
`hackbench` and `locks` are unchanged. The CPU added is 0.2 s on the installer N = 1 (+2.8%), 0.2 s on `build` N = 1 (+0.9%)
and 0.5 s at N = 4 (+2.0-2.3%); at N = 4 that assumes spare cores (four vCPU threads and four servers, eight busy threads on
this machine's ten). The installer and `build` N = 1 savings (1.20 and 1.66 s) are above the transport's bound
(3.5 us x compiles: 0.44 s and 0.8 s), as the per-compile numbers show. This session's machine ran slower than chunk 20's
(old installer N = 1 7.05 against 6.53 s then); the comparisons are interleaved. **Ship threshold met**: installer, `build`
and `zstd` faster beyond the spread at both sizes, nothing slower.

**Checks** (`suite21.sh`, `suite/`, the final tree with series58-channel, one VM runner at a time, each case's output deleted
as it ended): `cargo test` 108 + 2 ignored (host; the two new tests and the
ignored replay), abi 65, also with `FEXC` set; the resident stack audit `ok`; the Python unit tests 317; fixtures 64 and seed
44 at both block sizes; the i7 corpus at CPL 0 69 at both; faults 7,912 at both; `run_k1.py` 336 runs, no problem; `run_k2.py`
all ok, and `run_k2.py --only swapgs` (its `msr` at both block sizes, `fault`, `kicks`, `events`, `contain`) all ok;
`run_slice3_vm.py` `system`, `delivery`, `events`, `contain` (the killed server's runs fail in 0.02 s, the stopped one's at its
2 s deadline), `smp` 0 failures; `run_x3.py` 54/54 and every group (stress 6/6), `run_x4.py` 33/28/6, `run_l4a.py` (stress 2/2),
`run_l4b.py`, `run_s4.py` all ok; `run_boot_smoke.py` 4/4; `run_r1.py r1a` 7/7, `r1b` 12/12 (negative controls flagged); FEX
ASM 2,270: 1,898 pass, 48 precision deviations, 324 unsupported, no failure. The compiler's own checks (`checks/`): `test_serve.py` above, `test_fexc.py`,
`test_slice1.py`, `check_sites.py --slice1`, `verify_precision.py` all pass.

**Review** (independent, of the diff: no blocking defect). Applied, as series58-channel-r1: `os_sync` waits that fail with
EINTR, ENOMEM or EFAULT are retried on both sides (the header's advice; the deadline still bounds the host); the host polls
the server's stdout after every sleep that ends without the reply, not only on a timeout; the server takes stdin as closed only
when `poll` returns > 0; the host copies the request in and the reply out with volatile accesses to the mapping; the magic word
above; the server sums a reply's size in 64 bits and refuses one over the cap (or, on the pipe, over 2^32 - 1) with an error
reply; `--channel` takes a decimal fd only, and `--channel-reply-cap N` (tests only) lowers the reply cap. New tests:
`channel_caps_requests` (an empty request and one over 1 MiB refused, one of exactly 1 MiB posted), `channel_wakes_both_sides`
(with the real server: a server asleep after 30 ms idle answers within 10 ms, a host asleep through fork mode's 0.5 ms compiles
has its replies within 2.5 ms, a 1 MiB request reaches the server), a magic case in `channel_refuses_malformed_replies`, and
`tools/fexc/test_channel.py` (the server's side: 10 replies equal the pipe's in reuse and fork mode with the server asleep before
each; the over-cap error reply; exit on an empty request, on stdin's EOF while asleep, and with 3 on a request over 1 MiB; bad
`--channel` values, a small mapping and a wrong magic refused at start). New negative controls, red: the host's wake removed
(a sleeping server answered in 66 ms), the server's wake removed (the host's replies took 5.0-6.3 ms, its sleep slice); the
earlier ones again on r1 (the length bound removed: a crash reading past the mapping; the death check removed: both tests fail;
the deadline ignored: the test never ends; the reply copied one byte short: 77,394 replay differences). Rerun on r1: the
installer (126,369 requests) and `build` (229,738) streams recorded again at HEAD and replayed through the pipe and the channel,
also the installer's with `--report` and in fork mode: **0 differences** in each, and 0 over the pipe on both sides
(`replay20.py`); `test_serve.py` reuse, fork, against series57-fork's CLI; `test_channel.py`; `test_fexc.py`, `test_slice1.py`,
`check_sites.py --slice1`, `verify_precision.py`; `cargo test` 110 + 2 ignored (host), abi 65, with and without `FEXC`;
`run_x3.py` stress 6/6, `run_l4a.py` stress 2/2, `run_slice3_vm.py contain` 0 failures. Replies byte-identical, so the walls
were not measured again.

**Not done.** The channel's region size and spin bounds are constants (no setting); the pipe protocol stays for `test_serve.py`,
the replays and debugging, and the host no longer uses it. `test_serve.py` runs over the pipe only; the channel is covered by
the replays, `test_channel.py`, the unit tests and every VM run. No test guards against a fork on the spawn (one would have to
read the source); the stress cases are what caught it.

### Measured (chunk 22: a SYSCALL/SYSRET fast path in the resident, November 4; branch perf22-sysfast on 71aae61, series58-channel-r1)

Evidence and harness: `natemu-raw/sysfast3-20261104-01/` (README there). Base: 71aae61 (chunk 21) with its resident; the
change: this branch, the same host binaries (chunk 22 changes only the resident and `run_k2.py`). Compiler series58-channel-r1
for both; the `natemu` stub, cache off, `mitigations=off nokaslr`, 2 GiB, one VM at a time.

**The two trips today** (each guest system call is a SYSCALL trip and a SYSRET trip; costs per resident entry from chunk 17's
sub-buckets, a perf build, so with its accounting). A CPL-3 block ends before its SYSCALL with a literal exit to the
SYSCALL's RIP, which is never linked (its target is a system instruction), so every SYSCALL enters the resident; the kernel's
exit block (`...; swapgs; 7-byte NOP`) likewise ends with a literal exit to its SYSRET.
1. Gate and entry kind (31 ns with the linker's own work): `exception_capture` saves the native state (31 GPRs, 32 Q
   registers, FPCR, FPSR); `finish` copies the 800-byte frame into `Shared::captured` (before the bucket switch: the perf
   build charges it to `guest`); `returned` reads ESR/FAR/ELR/SPSR, disarms (nothing armed: one load), cancels a watch step
   and dispatches to the linker.
2. `load` and `bank` (23 ns): the whole state record (about 1.4 KiB) copied to the stack, then 16 GPRs, the flags (NZCV,
   PF/AF, DF; IF from the page at CPL 0), the GS bases at CPL 0, XMM0-15 and MXCSR's flags from the frame.
3. `block_at` and the link-point checks (7 ns): the block that ran the `svc` (PC hint), its REP self-exit, RF ends, the RIP
   is one of its link targets.
4. `find`, first pass: the boundary checks (12 ns: drain's load, the LAPIC poll, the trap/TF/DR7 test, `events::take`, the
   breakpoint and step tests, the code context, `select_l1`), the lookup (7 ns: the kept decode), then the instruction
   (27 ns with its accounting: `system`, `transaction`'s common checks, `syscalls::execute`, `commit`).
5. `find`, second pass at the target (IA32_LSTAR, or the return RIP): the same boundary checks (12 ns) and lookup (7 ns),
   the L1 table of the new context selected and the entry filled (already there).
6. The linker's link step: nothing to link (the target follows a system instruction).
7. `enter` (40 ns + 31 ns): the shadow, flag bytes, FS/GS bases, KERNEL_GS_BASE, FCW, TSC offset, SIMD and x87 bytes and
   the event-pending byte (18 ns); the native frame (10 ns); the state record written back (12 ns); the shadow-space switch
   at the CPL change (`tables::select`: TCR.EPD1 set, ISB, TTBR1, TTBR0, ISB, TCR restored, ISB; 31 ns); `resume_exception`
   and ERET.

Avoidable for these two kinds: the record's copy out and back (2 and 7: about 24 ns), the second pass's boundary checks (5:
12 ns, less the checks the instruction needs, a few ns), the captured frame (1: an 800-byte copy no literal exit or
dispatcher miss reads). Not avoidable: the gate, `bank` and the native frame (the instruction reads and writes GPRs and flags
and the target is entered from the frame), both lookups (the decode and the target must be current: the fetch-walk rule;
the fast path takes the decode only when it is current with no walk, see below),
`enter`'s per-entry writes (each is rewritten at every entry by the containment rules), and the shadow-space switch (the CPL
change switches TTBR, which only EL1 can do; each of its three ISBs orders a step the next relies on). Estimate, unmeasured:
about 40 ns per trip, 80 ns per system call, about 1.3 s of `hackbench` N = 1 (16 M system calls).

**Built** (resident only: `execution.rs`, `main.rs`, `cache.rs`, `execution/events.rs`, `execution/lapic.rs`; no abi, host
or compiler change):
1. **`sys_fast`**, called by `returned` for every `svc #LINK` before the linker. It works on the state record in place (the
   record is this vCPU's, EL1-only; nothing else reads it while the resident runs, and nothing on this path exits to the
   host), so it neither loads nor writes back a copy (`enter` skips the record write when given the record itself). It goes
   on only when, at the exit's boundary, `find`'s pass would find nothing to do: `State::debug` zero (no pending trap, step
   mark or data-breakpoint hits), TF clear, DR7 arming nothing (no instruction breakpoint, no watchpoint programmed), no MOV
   SS or STI shadow, not between REP iterations, no plain step, no wanted stop, nothing posted for this vCPU (drain's first
   load), no LAPIC expiry due and no reinjection backlog (`lapic::due`, `poll`'s test), no event `events::take` would take
   (`events::none`, after the bank, so IF is the block's); the exit is an ordinary one of a live block (not the step block,
   not a REP self-exit, the RIP one of its link targets); and the kept decode at the RIP is a SYSCALL or a SYSRET that is
   current, looked up with no walk (`cache::current_at`: a TLB entry in use, as the CPU fetches through one; after a CR3 load
   or TLB flush it is not current and the linker, which walks, runs it), and commits (EFER.SCE, no LOCK; SYSRET at CPL 0 with
   REX.W and a canonical RCX). Otherwise it returns and the linker runs as before; what it changed is what the linker then
   does again to the same effect (the bank from the same frame and page, RIP, RF cleared). Nothing before the commit walks
   or writes guest memory (the review's B1: with the walking lookup first built, a fetch walk writing an A bit into a page
   holding the exiting block killed it, and on a fall-back the linker then halted on a dead source, `bad_link`). The
   instruction runs as every system instruction does (`system`, `syscalls::execute`). Then, in place of `find`'s next pass, only what the instruction can change is looked
   at: the debug state (a TF SYSRET loads from R11 owes its trap before the first CPL-3 instruction) and the takeable events
   (SYSRET may set IF); either, or a target that is not a kept block (a miss, or a system instruction at the target),
   continues as `find` does after a system instruction (`after_system`: the record loaded, `find` from the next boundary, the
   linker's `stale_links` count for a target killed as placed or an exiting block gone). Otherwise the block at the target RIP
   (the lookup in the new context, as `find`'s: it may walk, after the commit) is put in the context's L1 table and entered
   (`enter`; `stale_links` counted as the linker counts it when that walk killed the exiting block).
2. **No captured frame for literal exits and dispatcher misses**: `finish` copies the gate's frame into `Shared::captured`
   only when the entry is not an `svc #LINK` or `svc #MISS` (`frame_only`): their services (`linker`, `sys_fast`, `missed`, a
   halt) read only the gate's frame; `captured` is read only by the paths that resume generated code mid-block (division,
   doorbell, aborts, splits, helpers, watch steps), which still get it. Every literal exit and miss gains, not only these two.
3. `perf` resident only: `Control::system_from` slot 26 (past the buckets; the host prints it as `"?"`) counts the trips the
   fast path ran to the end, and `Control::system_counts` slots 0x7c-0x7f (past every kind; the host prints them as kinds
   `0x7c`..`0x7f` in `k1.systems.kinds`) count why it did not: 0x7c no current decode (a CR3 load or TLB flush since its last
   use), 0x7d the boundary before it had something to do or the exit was not an ordinary one, 0x7e the decode current but not
   committing, 0x7f after the instruction a trap or event to take or a target not kept (`after_system`). Before the
   instruction a decline is counted only when the exit's RIP holds a kept SYSCALL or SYSRET decode, current or not (looked up
   with no walk, `cache::kept_at`). The default resident has no counter.

**Why it is exact** (SDM Vol.2 SYSCALL, SYSRET; Vol.3 7.9, 18.3.1; the cross-modifying-code rule). The instruction itself is
the same transaction as before (RCX/R11, RFLAGS & ~IA32_FMASK, the STAR selectors with the fixed attributes, CPL, IA32_LSTAR;
SYSRET's RFLAGS from R11, its CS/SS, CPL 3); the fast path only decides, before it, that it commits (a #UD without SCE or
with LOCK, SYSRET's #GP(0) at CPL > 0 or for a non-canonical RCX, and SYSRETL's refusal all leave by the linker, so no fault
is ever delivered on the in-place path). The boundary before it is checked as `find` checks it, with nothing to do: the same
predicates (trap, TF, breakpoint, shadow, events, timer, invalidations), so the same instruction runs at the same boundary.
The decode is used only when current with no walk; one not current (a CR3 load or TLB flush since) goes to the linker,
whose lookup walks the fetch and sets its A bits as the CPU's TLB fill would.
At the boundary after it, `find` would take a trap the instruction left (SYSRET loading TF: the debug state; SYSCALL leaves TF
only if it was set, which the first check excludes) and events now takeable: these are checked and fall back. What the fast
path does not repeat there is the drain and the LAPIC poll: neither instruction posts, writes memory or arms the timer, and
anything posted or falling due after the first check rings this vCPU's doorbell (every post is followed by a kick; the
virtual timer is armed at the deadline), which is masked in the resident and taken when the target block starts: at its
first word the vCPU stops there at once and `find` drains and polls at that boundary, as for any post landing after `find`'s
last look before an entry today (a block starting with a REP string arms a stop instead, as today). Neither SYSCALL nor
SYSRET is serializing, so a peer's code write may become visible at any later boundary. No event can become takeable by the
instruction but through IF (SYSCALL can only clear it; NMI blocking, TPR and the shadows are untouched), which is checked.
Instruction breakpoints at either target and data breakpoints anywhere keep DR7 non-zero, so the fast path never runs while
one is armed; TF at the SYSCALL's start, or a trap pending from the instruction before it, keeps the debug state non-zero.

**Tests.** `run_k2.py sysfast` (natemu, judged by the SDM): N = 2,000 SYSCALL round trips from CPL 3 into a
Linux-shaped kernel side, with the program's own IDT (gates on IST 1 of the driver's TSS), its handlers counting and recording
their first frame; one special round K = 1,000, when every record and block is kept: `sysret-irq` (an interrupt posted
while IF = 0 in the kernel: taken at the first CPL-3 boundary after the SYSRET, frame RIP the return RIP, once),
`sysret-tf` (R11 with TF: #DB with BS before the first CPL-3 instruction), `lstar-break` and `ret-break` (instruction
breakpoints at IA32_LSTAR and at the return RIP: a #DB fault at every round), `watch` (a data breakpoint on the quadword the
loop writes right before its SYSCALL: the trap at the SYSCALL's RIP every round), `syscall-sce0` and `sysret-sce0` (EFER.SCE
cleared: #UD at the next SYSCALL at CPL 3, resp. at the SYSRET at CPL 0, once), `sysret-noncanonical` (RCX = 2^47: #GP(0) at
the SYSRET, once), `kicks-events` (200,000 rounds under interrupts posted at random times: every delivery at CPL 3 with IF =
1, several hundred); every case ends with both counters at N. Added after the review (`sysfast_more`): `walk-ad` (identity
space, the guest's own tables: after round K points a second linear page L2 of the user code's frame F at F itself as its
page table, PTE(L2) with A = 0 there, and reloads CR3, the user loop at L1 jumps directly to the SYSCALL at L2; the walk
that revalidates its decode writes the A bit into F, killing the decode and the exiting block: every round must complete),
`user-spin` (an interrupt posted with IF = 0 in round K's kernel; after the SYSRET the user side spins, with no system
instruction, on a flag only the handler sets, at most 50 M times: the interrupt must be taken at the first CPL-3 boundary),
`peer-write` (2 vCPUs: vCPU 1 flips the immediate of the `mov eax` at vCPU 0's return RIP every 20 rounds, 300 times, raises
a flag and waits for it to be seen; vCPU 0, on the flag, runs CPUID and its next round must load the new value, SDM Vol.3
9.1.3), `apic-timer` (the LAPIC timer periodic at 10 ms for 0.1 s of round trips with a two-instruction IF = 1 window in the
kernel: every interrupt at CPL 3 or at the window's two interruptible boundaries, with IF = 1; consecutive deliveries 0.5-1.5
periods apart and their number the periods elapsed +-1), `rep-target` (every round's SYSRET returns to a REP MOVSB, kept from round 2 on; in round K, RCX = its
return RIP 0x1000 the count, with an interrupt posted in the kernel: taken before the first iteration, RCX, RSI, RDI
untouched, the 4 KiB copy then right), `nmi-sysret` (an NMI posted in round K's kernel taken at the next boundary; its
handler posts a second one and returns without IRET: the second stays blocked across the SYSRET and every later round trip
until round 1,500's kernel IRETQs to itself, and is taken right after that IRETQ), `div-frame` (the return RIP is a
`div r10` through fexc's division helper, every 4th round with RDX = 5: #DE with the frame naming the DIV at CPL 3, RAX and
RDX unchanged, the other registers live; the sums of quotients and remainders the SDM's). Existing cases that now run the fast path: `run_k2.py swapgs
kicks` (200,000 round trips under 20,000 precise stops), `rollback`, the `syscall` cost row, `run_s4.py s4-sysret`,
`run_slice3_vm.py system`'s S3 programs. Negative controls (`nc/mutant-*.diff`, expected red): the event check after the
instruction removed (`sysret-irq`: the interrupt taken before the next SYSCALL), the debug check after it removed
(`sysret-tf`), DR7 not looked at (`lstar-break`, `ret-break`: one #DB, not N), no debug state and the step block's exit
taken (`watch`: the trap after the SYSCALL); and the review's two (`nc/mutant-frame-copy-skipped.diff`: `frame_only` also
skips the captured frame for the division helper's entry, so its resume restores a stale frame: `div-frame`;
`nc/mutant-walking-lookup.diff`: the decode looked up with the walking `cache::lookup` again: `walk-ad`, a `bad_link`
halt). Also expected red with the event check after the instruction removed: `user-spin` (the spin runs out) and
`rep-target` (the interrupt not taken before the first iteration). Not controls by construction: the commit pre-checks (the transaction makes the
same checks and would deliver the fault in place, which they only keep off this path), the pre-instruction event check (a
post during the block stops it at a patched exit, which enters `stopped`, not the linker; one landing just as its `svc` is
taken is not reproducible on demand), the STEP and stop-wanted tests
(defence in depth: the other conditions already exclude those exits).

**Test results** (`tests/`, `suite/`; the first pass of the chunk's cases is kept in `tests-pass1/`). `run_k2.py --only
sysfast,rollback,swapgs`: 26 of 26 ok with this branch and 26 of 26 with the base (the cases are SDM behaviour); `run_s4.py`
and `run_slice3_vm.py system` ok. Negative controls, each run under `run_k2.py --only sysfast`, every expected case red and
every other green: event check after the instruction removed: `sysret-irq` (taken at the next SYSCALL), `user-spin` and
`rep-target` (taken after the REP had run: RCX 0); debug check after it removed: `sysret-tf`; DR7 not looked at: `lstar-break`,
`ret-break` (one #DB, not 2,000); no debug state and the step block's exit taken: `watch` (1,999 of 2,000 traps after the
SYSCALL), `lstar-break`, `ret-break`; `frame_only` skipping the division helper's frame: `div-frame` (2,499 wrong frames or
registers); the walking pre-commit lookup restored: `walk-ad` (`bad_link` at round 1,001). The first pass found two cases that
could not catch their control: `watch` recorded only the first #DB frame, and the first round runs the slow path (the
mutant's 1,999 wrong traps passed: perf counters showed 1,999 fall-backs after the SYSCALL); `rep-target` returned to the REP
only in round K, so its block was new there and the fast path fell back before entering it (one fall-back, counted). Both
cases were strengthened (every #DB frame's RIP checked; the REP the return target in every round) and the whole set rerun.
Standing suite (`suite22.sh`, then `suite22b.sh` for the cases the worktree lacked inputs for: the generated slice-1 fixtures,
copied from the main tree, manifest identical, and FEX's ASM tests, read from the main tree): `cargo test` 110 + 2 ignored
(host), abi 65, also with `FEXC`; the resident stack audit `ok` (16,560 / 20,736 / 8,800 bytes, unchanged); the Python unit
tests 299 OK (1 skipped); fixtures 64 and seed 44 at both block sizes; the i7 corpus at CPL 0 69 at both; faults 7,912 at
both; `run_k1.py` 336 runs, no problem; `run_k2.py` all ok (the 16 `sysfast` cases twice); `run_slice3_vm.py` `system`,
`delivery`, `events`, `contain`, `smp` 0 failures; `run_x3.py` 54/54, `run_x4.py` 33/28/6, `run_l4a.py`, `run_l4b.py`,
`run_s4.py` all ok; `run_boot_smoke.py` 4/4; `run_r1.py r1a`, `r1b` ok (negative controls flagged); FEX ASM 2,270: 1,898
pass, 48 precision deviations, 324 unsupported, no failure.

**Measured** (`meas/`). Round trip (`rtcost.py`, one vCPU, slope of the driver's wall time, three repetitions, us per trip),
base / chunk 22: Linux-shaped kernel side 0.689 / 0.565, bare SYSRET 0.374 / 0.344, two SWAPGS 0.693 / 0.552 (the `work` row
stops with a delivered fault in both builds: a defect of that row's program, not looked into). `run_k2.py cost` `syscall`
row 0.668 / 0.570 us; the other rows unchanged (CPUID 0.261 / 0.264, `div` 0.117 / 0.110). Perf resident, `hackbench` N = 1,
whole run: 31,976,288 of the 32.0 M SYSCALLs and SYSRETs ran the fast path to the end (99.9%); declines: no current decode
28,949 (after CR3 loads: each then takes the old path once), the boundary had something to do 557, not committing 0, a
fall-back after the instruction 158. `locks` N = 1: 6,250,774 of 6.26 M; declines 663, 107, 0 and 93. Logged wall (perf
resident) `hackbench` N = 1 15.04 -> 13.09 s, `locks` N = 1 6.16 -> 5.72 s. Sub-buckets, `hackbench` N = 1 (seconds of the
whole run, base / chunk 22): the record write in `enter` 0.50 / 0.03, `find`'s boundary checks and the fast path's checks
after the instruction 1.22 / 0.47, `load` and `bank` 0.92 / 0.76, the linker's own work 0.78 / 0.65, generated code (it
carries the gate's frame copy) 7.04 / 6.62, the shadow-space switch 1.26 / 1.30, the transactions 1.82 / 1.83; vCPU CPU
17.06 / 15.22 s, about 57 ns less per trip over 32.0 M trips.

Benchmarks (`camp/`, `campaign22.sh`, `summ22.py`: three interleaved rounds, order alternating, every run verified or
reached and uncontended; median (min-max), seconds; CPU s the process's, its fexc servers included, mean):

| Benchmark | N | before (71aae61) | after | change | CPU s before / after |
| --- | --- | --- | --- | --- | --- |
| `hackbench` | 1 | 13.85 (13.80-14.14) | **12.19** (11.85-12.24) | -12.0% | 16.9 / 15.0 |
| | 4 | 3.88 (3.83-3.91) | 3.78 (3.44-3.85) | -2.7% | 19.0 / 18.4 |
| `locks` | 1 | 5.81 (5.77-5.90) | **5.47** (5.32-5.49) | -5.9% | 8.7 / 8.3 |
| | 4 | 7.00 (6.96-7.06) | 6.84 (6.77-6.96) | -2.4% | 31.3 / 30.8 |
| Installer, cache off | 1 | 5.80 (5.76-5.91) | 5.87 (5.72-6.12) | +1.2% | 7.3 / 7.4 |
| `build` | 1 | 12.84 (12.79-13.12) | 13.11 (12.89-13.22) | +2.2% | 17.5 / 17.8 |
| `zstd` | 1 | 1.91 (1.90-1.99) | 1.96 (1.90-1.97) | +2.7% | 4.9 / 5.0 |
| `luajit` | 1 | 5.49 (5.39-5.59) | 5.67 (5.58-5.74) | +3.3% | 8.7 / 9.0 |
| `mem` | 1 | 9.89 (9.84-10.12) | 10.25 (9.92-10.29) | +3.6% | 13.0 / 13.2 |
| `cray` | 1 | 0.42 (0.41-0.43) | 0.41 (0.40-0.43) | -2.4% | 3.3 / 3.3 |

`hackbench` and `locks` at N = 1 are faster beyond their spreads; at N = 4 both medians are lower with overlapping ranges. The
five other cells but `cray` have higher medians (+1.2% to +3.6%), each range overlapping the base's. Every literal exit,
not only one to a SYSCALL or SYSRET, now runs `sys_fast`'s checks first (the bank and an event check among them, before the
decode lookup) and then, for any other target, the linker's own path as before.

**Decode first** (a24fc30): the decode is probed before anything else, so every other literal exit declines after one
probe and goes to the linker. Tests rerun (`tests2/`): `run_k2.py --only sysfast,rollback,swapgs` 26/26; each of the six
variants rebuilt on it turns only its expected cases red. Benchmarks, base 71aae61 against a24fc30 (`camp2/` rounds 1-2,
`camp3/` rounds 3-5, `camp4/` ten more `cray` and four more installer rounds; `camp2`'s third round stopped when its run was
interrupted, its two finished base runs kept; every run verified or reached and uncontended; median (min-max), seconds):

| Benchmark | N | before (71aae61) | after (a24fc30) | change | CPU s before / after | runs |
| --- | --- | --- | --- | --- | --- | --- |
| `hackbench` | 1 | 13.57 (13.13-13.85) | **11.56** (11.49-11.70) | -14.8% | 16.3 / 14.4 | 6 / 5 |
| | 4 | 4.02 (3.89-4.12) | **3.62** (3.25-3.71) | -10.0% | 19.5 / 17.8 | 5 / 5 |
| `locks` | 1 | 5.63 (5.46-5.77) | **5.20** (5.17-5.28) | -7.7% | 8.4 / 8.0 | 5 / 5 |
| | 4 | 7.07 (6.90-7.24) | 7.08 (6.92-7.22) | +0.2% | 31.7 / 31.7 | 5 / 5 |
| Installer, cache off | 1 | 5.50 (5.46-5.73) | 5.53 (5.47-5.92) | +0.5% | 7.0 / 7.0 | 10 / 9 |
| `build` | 1 | 12.55 (12.33-12.64) | 12.55 (12.46-12.61) | 0.0% | 17.0 / 17.0 | 5 / 5 |
| `zstd` | 1 | 1.92 (1.92-1.93) | 1.91 (1.90-1.96) | -0.3% | 4.8 / 4.8 | 5 / 5 |
| `luajit` | 1 | 5.40 (5.30-5.41) | 5.34 (5.31-5.38) | -1.1% | 8.5 / 8.5 | 5 / 5 |
| `mem` | 1 | 9.71 (9.52-9.84) | 9.65 (9.49-9.66) | -0.5% | 12.6 / 12.6 | 5 / 5 |
| `cray` | 1 | 0.41 (0.36-0.43) | 0.41 (0.38-0.43) | +0.2% | 3.2 / 3.2 | 15 / 15 |

`hackbench` at N = 1 and 4 and `locks` at N = 1 are faster beyond their spreads; nothing is slower (the other cells within
0.5% by median, ranges overlapping). Ship threshold met. The suite above ran on 742d949; a24fc30 only reorders `sys_fast`'s
checks (the probe first) and was judged by the `sysfast`/`rollback`/`swapgs` groups, the variants and an independent review
of the reordering.

**Not done.** The shadow-space switch keeps its three ISBs (the last could rely on ERET's context synchronization where
SCTLR_EL1.EOS makes ERET one; not built, unmeasured). `bank`'s and `enter`'s XMM moves stay (the instruction leaves XMM as it
is, but the frame is rebuilt from the bank). A SYSCALL reached by a computed exit (through the dispatcher's miss) takes the
general path.

### Measured (chunk 23: a metadata arena and block index twice as large, November 5; uncommitted on 5554c12, series58-channel-r1)

Evidence: `natemu-raw/flush-20261105-01/` (README there). Setup as chunk 18: the `natemu` stub, cache off,
`mitigations=off nokaslr`, 2 GiB, compiler series58-channel-r1 (unchanged), one VM at a time; the initramfs of
`bench-20261104-02`. Counts come from single runs with the perf resident and its log (request lines kept for the analysis,
then deleted); walls from uninstrumented runs.

**Measured first** (HEAD, with scratch counters: `scratch-census.diff`, not kept; at each whole-cache flush the resident
walks its block records and the host prints them; `refill.py` classes each translation request of the window by whether
this vCPU asked for the same key, RIP, bytes and context, in an earlier flush generation).

| At HEAD, one perf run each | `build` N = 1 | `build` N = 4 (per vCPU) | Installer N = 1 | Installer N = 4 (per vCPU) | `luajit` N = 1 | `mem` N = 1 |
| --- | --- | --- | --- | --- | --- | --- |
| Whole-cache flushes in the window, cause | 5, the metadata arena full each time | 1 each, the arena | 4, the arena | 1 each, the arena | 0 | 0 |
| Blocks at each flush (records 64 MiB; code of 120 MiB) | 238-244 k (35-36 MiB) | 241-242 k (35-36 MiB) | 240-241 k (36 MiB) | - | - | - |
| Dead blocks at each flush | 37.8 k (9.6 MiB) at the first, **0** at the other four | 0.9-34 k | 38.8 k at the first, 0-3 at the others | - | - | - |
| Live, not current / current | 186-222 k / 15-23 k | 194-225 k / 14-24 k | 191-225 k / 12-26 k | - | - | - |
| Requests repeating a key of an earlier generation (refills), host request time | **498.6 k, 0.73 s** (99.8% from the shared cache) | 487.0 k over the four, 0.83 thread-s | 99.0 k, 0.15 s | 76.9 k over the four, 0.13 thread-s | 0 | 0 |
| New keys in the window / distinct keys over the run | 246 k / 328 k | 647 k / 197-221 k per vCPU | 211 k / - | 379 k / - | 27 k / 111 k | 19 k / 103 k |

Every capacity flush is the arena (as chunks 6 and 18 found). The index (256 Ki blocks) would fill a few thousand blocks
later; the code area holds a third of its room. Nothing is dead after the boot's first flush: `build`'s programs stay in
the page cache, so no code page is ever written, and the nine in ten blocks not current are other processes' code (the
compiler and the shell), asked for again by the next process after each flush. Reclaiming dead records or compacting the
arena would free nothing; evicting what is not current would drop exactly what comes back. A whole-cache flush replayed on
`build` N = 1's request stream (`simflush.py`; it counts only requests, so a capacity of 150 k stands for the 64 MiB
arena: it gives 492 k refills against the 498.6 k measured) leaves 128 k refills at 1.5 times the capacity, 72 k with one
flush at twice and none at 2.7 times; at N = 4 each vCPU's stream has no flush at twice the capacity.

**Built** (abi `cache`; no resident, host or compiler code change): the metadata arena 64 to **128 MiB** (all the address
room below the lookup area) and L2 1 to 2 Mi entries, so the index holds **512 Ki blocks** (half of L2, as before); the
link records stay at 256 Ki (`MAX_LINKS` no longer follows the index). The lookup area grows from 20 to 30 MiB per vCPU;
every vCPU's window stays inside the 1 GiB identity map (the compile-time check: 872 of 1,024 MiB, from 650). Block numbers,
arena offsets and code offsets still fit their 32-bit fields. Every invalidation rule is unchanged: the cache keeps more
blocks, by the same rules, and still flushes whole when the arena, the index or the code area is full.
Tried and not kept: four times the linear map's buckets (64 Ki to 256 Ki): `build` N = 1's page-fault time 216 to 199 ms,
the chains it walks 1.95 to 1.59 M blocks (with the larger cache; 0.40 M at HEAD): most of a chain is blocks on the same
linear page.

**After** (one perf run each): `build` N = 1 flushes once (at 4.05 s of 11.6 s, 486 k blocks, 128 MiB of records, 80 MiB
of code), refills 138.3 k (0.21 s of host request time); `build` N = 4 no flush; the installer N = 1 two flushes, refills
48.8 k (0.08 s); N = 4 no flush. Costs seen on `build` N = 1: page-fault time 140 to 216 ms (the linear chains walked 0.40
to 1.95 M blocks), two link-record fills (456 k links undone, as chunk 11 bounds them), flush time 124 to 38 ms; CR3 loads
and TLB flushes 218 to 211 ms.

**Measured** (wall s, uninstrumented; `camp/`, `camp-extra/`, `campaign.sh`, `summ23.py`: three interleaved rounds, the
order alternating, plus three more of the cells slower by median; installer N = 4 once; `build` and the benchmarks from
START to END; CPU = the process's user + system, its servers included, mean; max RSS the process's, mean; every run
verified or reached the dialog, none contended):

| Workload | N | wall s, HEAD | wall s, chunk 23 | change of medians | CPU s, HEAD / 23 | max RSS MiB, HEAD / 23 | runs |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `build` | 1 | 12.63 (12.55-12.70) | **11.39** (11.39-11.51) | -1.24 s (-9.8%) | 17.1 / 15.9 | 1,192 / 1,304 | 3 / 3 |
| `build` | 4 | 5.62 (5.56-5.96) | **5.43** (5.36-5.48) | -0.19 s (-3.3%) | 23.4 / 21.7 | 1,656 / 1,799 | 3 / 3 |
| installer | 1 | 5.52 (5.46-5.67) | 5.42 (5.32-5.58) | -1.7% | 7.0 / 6.9 | 564 / 676 | 6 / 6 |
| installer | 4 | 4.60 (one run) | 4.50 (one run) | -2.2% | 9.0 / 8.9 | 955 / 1,116 | 1 / 1 |
| `luajit` | 1 | 5.35 (5.29-5.44) | 5.37 (5.34-5.43) | +0.2% | 8.5 / 8.5 | 1,059 / 1,069 | 6 / 6 |
| `mem` | 1 | 9.59 (9.54-9.66) | 9.56 (9.54-9.69) | -0.3% | 12.6 / 12.6 | 1,021 / 1,027 | 3 / 3 |
| `hackbench` | 1 | 11.72 (11.54-11.81) | 11.70 (11.58-11.89) | -0.1% | 14.4 / 14.5 | 870 / 878 | 3 / 3 |
| `locks` | 1 | 5.25 (5.24-5.28) | 5.20 (5.16-5.33) | -1.0% | 8.0 / 8.0 | 868 / 876 | 3 / 3 |
| `zstd` | 1 | 1.91 (1.90-1.96) | 1.91 (1.88-1.96) | -0.1% | 4.8 / 4.8 | 984 / 992 | 6 / 6 |
| `cray` | 1 | 0.41 (0.37-0.43) | 0.42 (0.37-0.43) | +0.4% | 3.2 / 3.2 | 874 / 882 | 6 / 6 |

`build` is faster beyond its spread at both sizes; the installer's medians are lower with overlapping ranges; the other
cells move within their spreads (`luajit` and `cray`, slower by median after three rounds, were given three more).
**Resident memory**: +112 MiB at N = 1 and +143 to +161 MiB at N = 4 where the arena now fills past 64 MiB (`build`,
the installer: more records, more code kept, the larger L2), +6 to +10 MiB elsewhere (the larger L2, cleared at each reset).
The walls were measured with `bin/a`, the tree before its comments were written (the shipped tree's resident differs only
in symbol hashes).

**Tests.** `run_slice2_probes.py`'s two capacity cases, resized (800,000 one-instruction ADDs fill the index first,
2,400,000 `add rax, [rsi]` in blocks of three the arena first, each three or more times over two trips) and now also
requiring the full structure filled to its capacity (the arena within one record); their code-cache argument corrected
for chunk 19's 8 MiB head (it was refused at setup since then: the runner is not in the standing suite). Green
(`cap/green.txt`): the index case 1,600,008 requests, 4 flushes, `full` [0, 0, 3], peak 524,288 blocks; the arena case 4
flushes, `full` [0, 3, 0], peak 134,217,632 bytes (479,470 blocks, 70 MiB of code). Negative controls (`nc/`, each a
one-line change to the resident's bound, built and run by `cap23.py`), each red on its own case only: the index bound
raised by 4,096 (a host error, "misaligned record": the extra index entries lie in the link records' area), the arena
bound raised by 1 MiB (a host error: a record read out of the area's bounds), the index bound lowered by 4,096 (peak 520,192, not filled), the arena bound lowered by
1 MiB (peak 133,169,016, not filled).

**Checks** (`suite23.sh`, `suite/`, the final tree, one VM runner at a time, each case's output deleted as it ended):
`cargo +1.97.0 test --offline --locked` 110 + 2 ignored (host), abi 65, also with `FEXC` set; the resident stack audit
`ok`; the Python unit tests OK; fixtures 64 and seed 44 at both block sizes; the i7 corpus at CPL 0 69 at both; faults 7,912
at both; `run_k1.py` 336 runs, no problem; `run_k2.py` all ok (74 cases), and `--only sysfast,swapgs` 22 of 22;
`run_slice3_vm.py` `system`, `delivery`, `events`, `contain` (453 controls), `smp` 0 failures; `run_x3.py` 54/54 and every
group, `run_x4.py` 33/28/6, `run_l4a.py`, `run_l4b.py`, `run_s4.py` all ok; `run_boot_smoke.py` 4/4; `run_r1.py r1a`,
`r1b` ok (negative controls flagged); FEX ASM 2,270: 1,898 pass, 48 precision deviations, 324 unsupported, no failure;
`run_slice2_probes.py` 85 match (the two capacity cases above included).

**Seen once, not explained.** One installer boot (HEAD's resident with the census counters, perf build, the chunk's first
run) did not reach the dialog: the guest's `main-menu` died of a segfault right after `localechooser` was selected (its
serial is kept, `census/census-inst-n1.serial`); the same build's next boot and the 22 other installer boots of this chunk,
both builds (campaigns and perf runs), reached it. Not reproduced or investigated further.

**Not done.** `build` N = 1 still flushes once (138 k refills, 0.21 s of host request time): holding its whole run needs
about 2.7 times HEAD's arena and index, past the identity map's room at four vCPUs. Smaller records (a block averages 276
bytes, sites 40 bytes each) or an eviction by age would be the next step; neither is built.

### Measured (chunk 24: breakdown at HEAD, November 5; analysis on 4afd86a, series58-channel-r1, no code change kept)

**Question.** Where the time goes at HEAD on the installer and the seven benchmarks, and three gates: per-address-space
copies (PCID), compiling a page's region in one request, and compiling ahead on idle cores; then a ranked list.

**Method** (`natemu-raw/breakdown-20261105-01/`, README there). HEAD's `natemu` stub, cache off, `mitigations=off nokaslr`,
2 GiB, bench-20261104-02's initramfs, one VM at a time. Chunk 18's method: three rounds of 14 cells (installer and `build`,
`hackbench`, `luajit`, `mem`, `cray` at N = 1 and 4; `zstd`, `locks` at N = 1) with the perf resident and `NATEMU_PERF_LOG`
(window: the installer from spawn to the end-point line, a benchmark from START to END), then one uninstrumented run per
cell. Three scratch builds, each with the default resident's loaded sections byte-identical to HEAD's (diffs kept, tree
restored): `count` (chunk 18's 100 ms counters, plus a perf-resident-only record of each CR3 load's root, each shadow install
and each revalidation), `count2` (the same with the corrections the PCID gate needed, below; one run per cell, and an
installer boot with Linux's default mitigations, PTI), and `prof` (a host PC sampler: each running vCPU forced out of the VM
every 150-250 us, the exit PC mapped to its block; default resident, no log; one run per N = 1 cell). Requests were analysed
from the request logs, which were then deleted.

**Checks.** Whole-run unattributed time (request time outside its named parts, chunk 1's method) is 1.0-3.8% of the worst
thread's window in every cell but `cray` N = 1 (5.6%: its whole run is nearly all boot; its window is 0.42 s); in the window
table the parts add up to the window by construction. Every run verified or reached the dialog, none contended. The log's
cost (uninstrumented against logged walls): installer 5.87 / 5.77 s (N = 1), 4.46 / 4.77 (N = 4); `build` 11.44 / 11.81,
5.36 / 5.75; `hackbench` 11.77 / 11.63, 3.35 / 3.43; `luajit` 5.34 / 5.56, 1.75 / 1.75; `mem` 9.74 / 9.76, 3.09 / 3.24;
`cray` 0.43 / 0.42, 0.24 / 0.25; `zstd` 1.93 / 1.95; `locks` 5.30 / 5.28.

| Seconds of the window, mean of 3 (N = 4: four vCPU threads summed) | inst 1 | inst 4 | build 1 | build 4 | hackb 1 | hackb 4 | luajit 1 | luajit 4 | mem 1 | mem 4 | cray 1 | cray 4 | zstd 1 | locks 1 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Wall, window (log on) | 5.77 (5.71-5.82) | 4.77 (4.70-4.85) | 11.81 (11.70-11.86) | 5.75 (5.63-5.83) | 11.63 (11.55-11.69) | 3.43 (3.34-3.51) | 5.56 (5.48-5.61) | 1.75 (1.74-1.77) | 9.76 (9.64-9.83) | 3.24 (3.20-3.28) | 0.42 | 0.25 (0.23-0.29) | 1.95 (1.94-1.98) | 5.28 (5.21-5.33) |
| Host: compile round trips (fexc) | 0.65 | 0.92 | 0.75 | 1.68 | 0.01 | 0.04 | 0.16 | 0.49 | 0.10 | 0.23 | 0.02 | 0.04 | 0.05 | 0.02 |
| Host: rest of the requests (admission, copies, staging, relocation, log hash) | 0.57 | 0.81 | 0.73 | 1.26 | 0.01 | 0.03 | 0.18 | 0.27 | 0.06 | 0.16 | 0.02 | 0.07 | 0.02 | 0.01 |
| Host: rest outside the VM (HVF entry/exit, exits, devices, log) | 0.54 | 0.76 | 0.57 | 1.08 | 0.01 | 0.03 | 0.18 | 0.25 | 0.06 | 0.17 | 0.02 | 0.07 | 0.02 | 0.01 |
| VM: generated guest code | 1.42 | 1.85 | 6.14 | 7.35 | 5.35 | 6.03 | 3.10 | 3.30 | 4.90 | 5.64 | 0.31 | 0.35 | 1.73 | 3.08 |
| VM: halted (and AP mailbox) | 0 | 11.55 | 0 | 6.81 | 0 | 0.27 | 0 | 0.61 | 0 | 1.34 | 0 | 0.37 | 0 | 0 |
| VM: linker entries, own work | 0.74 | 0.94 | 0.93 | 1.38 | 1.88 | 2.10 | 0.57 | 0.63 | 0.70 | 0.78 | 0.01 | 0.03 | 0.02 | 0.34 |
| VM: dispatcher misses, own work | 0.13 | 0.16 | 0.40 | 0.51 | 0.03 | 0.05 | 0.05 | 0.05 | 0.04 | 0.05 | 0 | 0 | 0 | 0.47 |
| VM: entering blocks and block lookup | 0.29 | 0.32 | 0.45 | 0.54 | 2.61 | 2.86 | 0.35 | 0.35 | 0.73 | 0.77 | 0 | 0.01 | 0.01 | 0.95 |
| VM: link patching, placement, in-VM reuse | 0.56 | 0.67 | 0.55 | 0.64 | 0.01 | 0.02 | 0.06 | 0.07 | 0.02 | 0.04 | 0 | 0.01 | 0 | 0 |
| VM: system instructions (transactions) | 0.13 | 0.14 | 0.08 | 0.09 | 1.47 | 1.61 | 0.32 | 0.31 | 0.58 | 0.58 | 0 | 0 | 0.01 | 0.35 |
| VM: CR3 loads and TLB flushes | 0.10 | 0.19 | 0.20 | 0.27 | 0.04 | 0.11 | 0.04 | 0.03 | 0.02 | 0.02 | 0 | 0 | 0 | 0 |
| VM: shadow misses (abort and walk) | 0.38 | 0.38 | 0.31 | 0.45 | 0.15 | 0.37 | 0.52 | 0.59 | 2.01 | 2.45 | 0 | 0 | 0.06 | 0 |
| VM: revalidation and fetch walks | 0.11 | 0.13 | 0.19 | 0.23 | 0.01 | 0.02 | 0.03 | 0.03 | 0.01 | 0.02 | 0 | 0 | 0 | 0 |
| VM: deliveries and IRQ polls | 0.09 | 0.13 | 0.25 | 0.35 | 0.02 | 0.03 | 0.02 | 0.02 | 0.47 | 0.54 | 0 | 0 | 0.01 | 0 |
| Before the resident ran; helpers, divisions, stops, L1 switches, shootdowns | 0.05 | 0.11 | 0.28 | 0.37 | 0.03 | 0.15 | 0 | 0 | 0.05 | 0.15 | 0.02 | 0.04 | 0 | 0.05 |

| Over the window, all threads, mean of 3 | inst 1 | inst 4 | build 1 | build 4 | hackb 1 | hackb 4 | luajit 1 | luajit 4 | mem 1 | mem 4 | cray 1 | cray 4 | zstd 1 | locks 1 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Compiles, k (distinct bytes; raced on another vCPU) | 126.0 (125.4; 0) | 129.7 (128.3; 1.1) | 149.3 (148.9; 0) | 163.0 (151.9; 10.9) | 2.5 | 4.3 (4.1; 0.2) | 26.6 (25.6; 0) | 31.6 (26.6; 5.0) | 20.2 | 24.3 (19.9; 4.4) | 3.7 | 4.9 (4.6; 0.3) | 7.4 | 3.3 |
| Shared-cache hits, k | 121.9 | 229.0 | 176.3 | 423.4 | 1.0 | 9.7 | 77.6 | 104.8 | 1.0 | 53.9 | 7.3 | 31.5 | 0.1 | 0.5 |
| Block entries / linker entries / dispatcher misses, M | 3.26 / 2.60 / 0.60 | 3.48 / 2.75 / 0.65 | 4.77 / 2.71 / 1.98 | 4.97 / 2.87 / 2.01 | 32.87 / 32.72 / 0.15 | 33.51 / 33.21 / 0.29 | 4.82 / 4.55 / 0.25 | 4.83 / 4.56 / 0.25 | 9.56 / 8.06 / 0.26 | 9.17 / 7.76 / 0.24 | 0.02 / 0.01 / 0 | 0.03 / 0.02 / 0.01 | 0.17 / 0.14 / 0.03 | 12.33 / 6.17 / 6.16 |
| System-instruction entries, M (largest kinds) | 1.27 (MOV from Sreg 0.32, STI 0.31, IRET 0.20) | 1.32 | 0.64 (STI 0.26) | 0.65 | 32.70 (SYSCALL, SYSRET 15.97 each) | 33.16 | 4.25 (VSTMXCSR 4.00) | 4.23 | 8.03 (STI 4.53, IRET 1.25, MOV from CR 1.24) | 7.65 | 0 | 0 | 0.13 | 12.33 (STI 6.16, SYSCALL, SYSRET 3.08 each) |
| Shadow installs, k | 598 | 590 | 692 | 756 | 244 | 551 | 1,167 | 1,187 | 6,431 | 6,756 | 1 | 1 | 228 | 1 |
| CR3 loads / page faults, k | 21.7 / 53.4 | 2.6 / 53.9 | 1.0 / 82.7 | 0.8 / 83.2 | 26.9 / 3.2 | 63.0 / 3.2 | 4.6 / 25.3 | 4.6 / 25.1 | 3.6 / 1,241 | 3.6 / 1,180 | 0 / 0.3 | 0 / 0.2 | 0.2 / 4.4 | 0 / 0.2 |
| Code-write invalidations / blocks killed / recompiles (new bytes at a RIP compiled before), k | 1.9 / 38.8 / 0.7 | 2.1 / 40.2 / 0.7 | 0 / 0.2 / 0.4 | 0 / 0.1 / 0.4 | 0 | 0 | 1.1 / 86.5 / 1.3 | 1.0 / 82.0 / 1.3 | 0 / 0.3 / 0.1 | 0 / 0 / 0.1 | 0 | 0 | 0 / 1.0 / 0 | 0 |
| Host exits (resident side), k; HVF entry and exit per exit, us | 313; 0.94 | 425; 0.97 | 332; 0.93 | 593; 0.98 | 5; 0.92 | 11; 0.93 | 104; 0.91 | 134; 0.95 | 31; 0.93 | 85; 0.96 | 8; 0.91 | 22; 0.94 | 10; 0.92 | 3; 0.91 |

Per compile request (installer N = 1; the other cells within 1 us at N = 1): 8.25 us, of which the fexc round trip 5.16,
admission 0.94, copies, lookup, staging and keeping 0.57, the rest 1.59 (relocation; with the log, the bytes' hash and exit
list); at N = 4 the round trip is 7.1 (installer) to 15.5 us (`luajit`). The servers' CPU per compile (wait4) is 11.9-12.2 us
at N = 1, of which their idle spin is about 4.0-4.9 us (the gap to the vCPU's next compile, capped at the 20 us spin bound);
FEX's own compile, from chunks 20 and 21's profiles, is about 3.6-4.7 us of the round trip. Shared-cache hit: 1.2-2.4 us.
All tables with ranges: `tables.md`; system instructions by kind there.

**What the numbers say.**
1. *Installer* (5.77 s): host requests 1.22 s (126 k compiles, nearly all of new bytes; 122 k shared hits), the rest outside
   the VM 0.54 s (313 k exits), generated code 1.42 s, the resident 2.59 s over a dozen items (linker 0.74, patching,
   placement and reuse 0.56, shadow misses 0.38). At N = 4 the four threads are halted 11.6 of 19.1 thread-seconds.
2. *`build`* (11.81 s): generated code 6.14 s (52%); requests 1.48 s and exits 0.57 s; the resident 3.6 s (linker 0.93,
   patching/placement/reuse 0.55, entering and lookup 0.45, misses 0.40). N = 4: halted 6.8 of 23.0 thread-seconds.
3. *`hackbench`* (11.63 s): 16 M system calls, each a SYSCALL and a SYSRET trip on the resident's fast path (32.7 M entries):
   linker 1.88, entering and lookup 2.61, transactions 1.47 s; generated code 5.35 s.
4. *`luajit`* (5.56 s; run-time code generation): code writes are few: 1.1 k code-write invalidations kill 86.5 k blocks, and
   1.3 k of the 26.6 k compiles are new bytes at a RIP compiled before; 78 k shared hits. The largest resident item is
   **4.00 M VSTMXCSR**, every one a resident trip (with its linker entry, lookup, entering and transaction, about 0.27 us
   each: about 1.1 s); shadow misses 0.52 s (1.17 M installs); generated code 3.10 s (56%).
5. *`mem`* (9.76 s; faults, mmap churn, fork): 1.24 M page faults, each with a delivery, a CR2 read and an IRET trip
   (deliveries 0.47 s), 6.4 M shadow installs (5.2 per fault; shadow misses 2.01 s), and **4.53 M STI** run by the resident
   (literal exits to an STI that starts its block: the inline STI of chunk 15 is never a block's first instruction), about
   1.07 s; generated code 4.90 s, of which the kernel's `clear_page` (`rep stosq`) is 16% of the sampled guest time.
6. *`cray`* (0.42 s; floating point): 74% generated code, 90% of the samples in blocks, three FP loop blocks 78% of them.
7. *`zstd`* (1.95 s): 89% generated code. *`locks`* (5.28 s): generated code 3.08 s; **6.16 M STI** run by the resident (via the
   dispatcher's miss: an STI right after a `ret`), 6.16 M fast-path system calls; misses, entering and lookup 1.4 s.

**Gate 1, PCID: per-address-space copies.** For each CR3 load the scratch counter finds whether its root was used before and
how recently (Linux keeps 6 dynamic ASIDs per CPU; with PTI it uses two roots per address space), and after it counts what a
per-root copy would have kept: shadow installs of a page this root filled before to the same frame (refaults), revalidations
of blocks last checked under the same root (each with the miss or linker entry that reached it), and the links undone and K1
time of the load. A first pass counted the build's new processes as returns: Linux frees and reuses a root page, so a root
page whose user-half entries changed is now a new address space (in `build` nearly every new address space, about 290, starts
on one), and a reload of the same root, a global flush, an INVLPG or a #PF ends what a copy keeps (`mem`'s refaults 2.96 M ->
21 k). Upper bound (copies cost nothing, and the guest's flushes for a returning address space are not seen).

| `count2`, one run each, window | inst 1 | inst 4 | build 1 | build 4 | hackb 1 | hackb 4 | luajit 1 | luajit 4 | mem 1 | mem 4 | zstd 1 | **inst 1, PTI** |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| CR3 loads to a root used before, among the 6 most recent / 6-11 / 12+, k | 19.9 / 0.3 / 0.1 | 1.0 / 0.1 / 0 | 0.4 / 0 / 0 | 0.3 / 0 / 0 | 0.8 / 1.2 / 24.2 | 24.3 / 16.7 / 36.0 | 4.2 / 0 / 0 | 4.1 / 0 / 0 | 1.5 / 0 / 0 | 1.5 / 0 / 0 | 0.1 | **260.1** / 1.9 / 1.0 |
| New address spaces / same root again, k | 0.8 / 0.5 | 0.6 / 0.4 | 0.3 / 0.2 | 0.3 / 0.2 | 0.1 / 0.1 | 0.5 / 0.1 | 0.1 / 0.2 | 0.1 / 0.2 | 0.8 / 1.2 | 0.8 / 1.3 | 0 | 1.4 / 0.6 |
| After a load to one of the 6: refaults user / kernel, k | 35.3 / 12.4 | 20.2 / 11.1 | 7.5 / 0 | 6.0 / 0 | 3.3 / 0 | 109.0 / 0 | 215.3 / 13.4 | 209.0 / 14.4 | 21.5 / 0 | 31.2 / 0 | 0.3 | **1,806 / 5,234** |
| Revalidations kept, via linker entries / via misses, k | 31.3 / 41.7 | 9.2 / 17.5 | 20.5 / 25.7 | 13.5 / 15.7 | 0.3 / 0.6 | 7.4 / 11.6 | 100.3 / 136.5 | 98.8 / 133.6 | 0.6 / 0.5 | 0.3 / 0.2 | 0.1 / 0.1 | **2,081 / 2,817** |
| Links undone at those loads, k; their K1 time, ms | 156; 34 | 188; 31 | 441; 86 | 447; 113 | 2; 1 | 48; 22 | 161; 27 | 162; 24 | 77; 11 | 77; 12 | 3; 0 | 3,189; 771 |
| **Estimate**: refaults x install (0.31-0.68 us) + kept revalidations x (walk + entry, 0.17-0.77 us) + K1 time, s wall | 0.09 | 0.04 | 0.11 | 0.05 | 0.00 | 0.03 | 0.19 | 0.06 | 0.02 | 0.01 | 0.00 | **5.87** |

With mitigations off the gate fails: at most 0.19 s (`luajit`, 3.5%), 0.09 s on the installer (1.6%), 0.11 s on `build`, and
nothing measurable elsewhere (`cray`, `locks`: no CR3 loads in the window). With Linux's default mitigations (PTI; Linux
sees no PCID) the installer takes **14.40 s uninstrumented against 5.50 s** with them off (15.40 s with the log); every system
call and interrupt switches between the two roots of an address space (260 k returning loads in the window), and the
per-root copies would keep 7.0 M shadow installs and 4.9 M revalidations: about 5.9 s of the 8.9 s PTI adds (upper bound; 9.4 s with the log;
the PTI boot's shadow misses are 4.62 s against 0.32 s, CR3 loads 0.98 against 0.10 s).

**Gate 2, compiling a page's region in one request.** From each compiled block not itself a pending successor (a root),
the request pass follows its exits on the root's page (link targets and a call's return site, as admission lists them),
through later requests of the same vCPU, to any depth; chains rooted in the window only (`count2` runs).

| Window, all threads | inst 1 | inst 4 | build 1 | build 4 | luajit 1 | luajit 4 | mem 1 | mem 4 | others |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Compiled roots / same-page successors asked for (by compile; by shared hit), k | 17.5 / 139 (108; 31) | 19.8 / 130 (110; 20) | 16.6 / 188 (130; 58) | 25.5 / 175 (141; 34) | 2.9 / 55 (23; 32) | 3.9 / 61 (25; 36) | 2.2 / 13 (13; 0) | 3.6 / 19 (17; 3) | <= 4 |
| Asked after the root's reply: < 10 us / 10-100 us / 0.1-1 ms / 1-10 ms / >= 10 ms, % | 8 / 24 / 26 / 12 / 31 | 9 / 27 / 31 / 16 / 16 | 6 / 17 / 20 / 9 / 49 | 9 / 23 / 30 / 12 / 26 | 4 / 14 / 19 / 22 / 41 | 5 / 11 / 21 / 20 / 44 | 12 / 37 / 29 / 12 / 10 | 13 / 36 / 29 / 13 / 9 | |
| Same-page candidates never asked for, k | 44.5 | 52.4 | 36.8 | 61.0 | 5.3 | 8.3 | 6.6 | 10.7 | |
| **Estimate**, s wall: requests saved x fixed cost, less never-asked x 3.6-4.7 us | 0.11-0.49 | 0.17-0.44 | 0.21-0.65 | 0.30-0.49 | 0.09-0.17 | 0.09-0.12 | 0-0.04 | 0.02-0.03 | < 0.03 |

Fixed cost per saved request: the exit (HVF 0.91-0.98 us + 0.1), the resident's request-side work (fetch walk 0.05-0.32 us,
the cheaper of the miss and linker entry), and the round trip less FEX's compile (5.0-15.5 - 3.6..4.7 us; at N = 4 this
includes the queueing of four vCPUs) plus the host's fixed parts (0.3 to 2.1-2.5 us); a shared hit saves its request less
admission (1.9-2.3 us). Admission and placement stay per block. Every successor is assumed to be in the region (the chain
depth is unbounded: 78% of `build`'s are at depth 4 or more, and 31-49% are asked 10 ms or more after the root). At most 8.5%
of the installer N = 1 wall and 5.5% of `build`'s.

**Gate 3, compiling ahead on idle cores (N = 4).** Halted against compile wait, thread-seconds of the window: installer 11.55
against 0.92 (fexc round trips), `build` 6.81 / 1.68, `luajit` 0.61 / 0.49, `mem` 1.34 / 0.23, `cray` 0.37 / 0.04, `hackbench`
0.27 / 0.04. There is room for the work: chunk 16's pool compiled about 0.87 ahead compiles per compile (at 16 us each:
1.8 and 2.3 s of CPU for the installer and `build`, well under their halted time). The saving is small: chunk 16 served 25% of
the compiles at N = 4 in time (installer 33.2 k of 132 k, `build` 62.3 k of 249 k; 58-61% of ahead compiles wasted; a
depth-1 successor is asked for 4.0-4.4 us after its predecessor's reply, here 5-13% of the N = 4 cells' same-page successors within 10 us).
**Estimate**: round trips x 0.25 / P (busy threads per wall second): installer 0.15 s, `build` 0.15 s, `luajit` 0.03 s, the
others under 0.02 s; chunk 16 measured no wall gain when round trips were 2-3 times today's. The gate fails.

**Generated code** (`prof`, one run per N = 1 cell; the sampler's share of EL1 samples is larger than the buckets' resident
share, so only its EL0 samples are compared). Of the EL0 samples, the dispatcher's table code (computed exits) is 15% on
`build`, 11-12% on the installer, `mem` and `locks`, 1-10% elsewhere; exits landing on a block-exit `svc` (the trap into the
resident) 15% on `hackbench`, 11% on `luajit`, 2-9% elsewhere; REP-string blocks 17% on `mem`, 1-4% elsewhere (`profcls.json`).
Hottest blocks (`prof/*.prof.md`, top 20 each, the top 8 disassembled with FEX's code):
- *`cray`*: three blocks are 78.5% of block samples (43-47 x86 instructions each, 1.8-1.9 Arm instructions per x86
  instruction); FEX maps the scalar and packed arithmetic one to one, and loads every `movsd`/`movupd` operand into a scratch
  register first and then moves all 128 bits to the destination (one `ldr` plus one `mov v.16b` per load).
- *`zstd`*: the top 20 blocks are 70% of block samples; the largest (165 x86 instructions, 4.3 Arm each) is 13%; in the
  match-finder loop blocks (3-7 Arm instructions per x86 one) each store carries its page-crossing check (`add`, `eor`, `tbnz`)
  and flags are materialised for later reads; `rep movsb` copies a byte per 7-instruction iteration (2.3%).
- *`build`*: flat: the top 20 blocks are 6% of 16 k block samples over 9.2 k distinct blocks; the kernel's `rep stosq` (27 Arm
  instructions, a 9-instruction loop per 8 bytes: the direction flag reloaded and the page checked every element) is first at
  1.1%.
- *`mem`*: that `rep stosq` is 18% of block samples. *`hackbench`*: the top blocks are the exits into SYSCALL and SYSRET.
  *`locks`*: `lock cmpxchg` is `dmb ish` and `casal`; the sample lands after the `casal`.

**Candidates** (estimates, not measurements; arithmetic in `estimates.txt`; s wall, N = 1 unless stated):

| # | Candidate: arithmetic | Saving, estimate |
| --- | --- | --- |
| 1 | **STI that starts its block, inline** (an event pending then needs the resident's slow path without re-entering the same block): resident-run STIs x a system-instruction trip (0.13-0.27 us, the cell's linker, lookup, entering and transaction shares) | `locks` 0.8-1.1 (16-21%), `mem` 1.07 (11%; N = 4 0.32, 10%), installer 0.15 (2.6%), `build` 0.15 (1.2%), `hackbench` 0.08 |
| 2 | **STMXCSR / VSTMXCSR inline at CPL 3**: 4.00 M x 0.27 us | `luajit` 1.09 (20%; N = 4 0.31, 18%) |
| 3 | **PCID with per-root copies**, for PTI (gate 1): kept refaults and revalidations | PTI installer 5.9 of 14.4 s (upper); mitigations off <= 0.19 (`luajit`), installer 0.09 |
| 4 | **REP STOS/MOVS in bulk** (the part of a string inside one page, DF read once): the `rep` blocks' sampled share of generated code x (1 - 1/4 .. 1/8) | `mem` 0.6-0.7 (17% of 4.90 s), `hackbench` 0.15-0.17, `build` 0.08-0.10, `zstd` 0.05, installer 0.03 |
| 5 | Page-fault path trips (CR2 read and IRET at CPL 0, 1.24 M each) x 0.235 us | `mem` 0.58 (6%) |
| 6 | Compiling a page's region in one request (gate 2) | installer 0.11-0.49, `build` 0.21-0.65; N = 4 0.17-0.44 and 0.30-0.49; `luajit` 0.09-0.17 |
| 7 | MOV from a segment register (installer 0.32 M) x 0.48 us | installer 0.15 |
| 8 | Compiling ahead on idle cores (gate 3) | N = 4: installer, `build` 0.15; others < 0.04 |
| 9 | Generated code (a bound only): the dispatcher's computed exits, per-store page checks, load-then-move, flags | the guest bucket: `build` 6.14, `hackbench` 5.35, `mem` 4.90, `luajit` 3.10, `locks` 3.08, `zstd` 1.73, installer 1.42, `cray` 0.31 |

**Recommendation.** Next chunk: the two system instructions the new benchmarks make the resident run millions of times, as
chunk 15 did for CLI, STI and RDTSC: an STI that starts its block, translated inline with a slow path for a pending event,
and STMXCSR/VSTMXCSR translated at CPL 3 (MXCSR's flags from the native status register and the kept ones): about 1 s each
on `locks`, `mem` and `luajit` (11-21%), 0.15 s on the installer and `build`; measured first by a scratch count of where the
resident-run STIs sit (block start after a literal exit or a `ret`) and whose VSTMXCSRs `luajit` runs. The chunk after: REP
STOS/MOVS in bulk within a page (`mem` 0.6-0.7 s, `hackbench` 0.15 s), or, if the owner brings PTI into the standing runs,
PCID with per-root copies (the only candidate measured above 1 s on a boot: about 5.9 s of the PTI installer's 14.4 s). Region
compiles and compile-ahead stay below 0.65 s and 0.15 s and are not recommended now.

**Not done.** No optimisation built; no instrumentation kept (the diffs and the scratch PCID module are in the evidence
directory; the tree is HEAD's). The PCID copies' own costs (switching per-root shadows, ASID management) are not estimated.
The sampler's EL1 share disagrees with the resident buckets (`hackbench` 70% against about 50%: a forced exit lands more often
at EL1), so its numbers are used only within EL0. The fill table of the PCID counter is 64 Ki entries: `mem`'s working set
overflows it (3.5 M collisions), so its refault count is a lower bound (its first-in-root installs, 79 k, bound the rest).

### Measured (chunk 25: an STI that starts its block, and STMXCSR/VSTMXCSR, translated inline, November 5; uncommitted on afb2897, series60-stmxcsr)

Evidence: `natemu-raw/inline2-20261105-01/` (README there). FEX: two commits on the fork clone's local branch `natemu-chunk25`
(from `natemu` 455698b5, not pushed): 09dd3ca19 (FexcInterruptFlagOp's comment: an inline STI may start its block; no code
change) and ab62c7e06 (STMXCSR, which VSTMXCSR dispatches to, stores a new op `FexcMxcsr` under slice 1). Compilers
series59-sti (09dd3ca19) and series60-stmxcsr (ab62c7e06), each with this tree's shim; natemu's submodule pin is unchanged.

**Measured first** (`meas/`; HEAD's code with scratch counters in a perf resident, `meas/scratch-count.diff`, not kept; one
run each, whole run with boot). Resident-run STIs, all at CPL 0, IF = 0 before every one:

| | `locks` 1 | `mem` 1 | `hackbench` 1 | `build` 1 | `build` 4 | installer 1 | `luajit` 1 | `zstd` 1 | `cray` 1 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| STIs run by the resident, k | 6,374 | 4,661 | 581 | 387 | 382 | 312 | 209 | 172 | 124 |
| reached by a literal exit / the dispatcher's miss, k | 122 / 6,252 | 4,574 / 87 | 548 / 33 | 374 / 13 | 368 / 14 | 298 / 15 | 200 / 9 | 165 / 7 | 122 / 2 |
| an event held there (IF = 0, one pending) | 399 | 672 | 989 | 213 | 366 | 194 | 87 | 81 | 58 |
| next instruction a block / a system instruction, k | 6,373 / 0 | 4,660 / 0 | 580 / 0 | 387 / 0 | 379 / 2.7 | 312 / 0 | 209 / 0 | 172 / 0 | 124 / 0 |

None followed another system instruction in the same lookup; the rest (a few hundred) were shadow steps with an event
pending. So every one is first in its block and the chunk-15 rule "never first" alone keeps it the resident's: the next
instruction is always translatable. In this kernel 561 of its 569 STI sites follow a `call` (static count over
`vmlinux.elf`), so the STI starts the block entered when the call returns (Linux's `local_irq_enable()` after an out-of-line
call); `locks` reaches it by `ret` (the dispatcher's miss), `mem` and the rest by literal exits. VSTMXCSR: 4,000,000 on
`luajit` N = 1, all at CPL 3 and reached by literal exits, STMXCSR 0-1 elsewhere;
LDMXCSR and VLDMXCSR 2.2-2.5 k per run on every workload, so LDMXCSR is not built. The run-time decodes put the VSTMXCSRs at
two RIPs of the statically linked `luajit`, each `vstmxcsr [rsp+0x18]; mov edx, [rsp+0x18]; ...; and edx, 0x6000; jne`:
glibc libm's rounding-mode guard (SET_RESTORE_ROUND: read MXCSR, LDMXCSR only when RC is not round-to-nearest) in its FMA
`sin`/`cos` (the `|x|` high-word test follows); the same guard is in 36 VSTMXCSR and the STMXCSR sites of the dynamic
`libm.so.6` and in glibc's `fe*` functions. Estimate (chunk 24): `locks` 0.8-1.1 s, `mem` 1.07 s, `luajit` 1.09 s; go.

**Built.**
1. **STI first in its block** (shim `Cut`, admission, host, abi, resident).
   - fexc: under `--cpl0` the one-byte STI is translated wherever the next instruction is (`Follows`: not last, not before
     a REP string, LOCK NEG or a system instruction), now also as its block's first instruction. Its code is chunk 15's:
     a load of the event-pending byte; set, a literal exit to its own RIP (here the block's own start); clear, IF = 1.
   - Admission (`interrupt_flag`) admits an STI at guest offset 0 and returns it as the block's `sti_first`; the host passes it
     with the reply (`State::sti_first`); the resident marks the block (`cache::STI_FIRST`, kept by the in-resident copies of
     chunk 10 and compared by their check).
   - `find`: a found block (kept, copied or just placed) marked STI_FIRST is not entered when its STI would take the slow
     path at this very boundary or must not run as its first instruction: IF = 0 with an event held (`events::held`, what
     `enter` would write into the pending byte), a MOV SS or STI shadow (entering would end it before the STI completes,
     and the STI's exit to its own start would then put a boundary inside it), or RF set (that exit would clear RF with no
     instruction completed). There the resident runs the STI itself (`system`, as its kept decode ran before: shadow, the
     step of the next instruction, the delivery) and the search goes on after it.
   - Linker: a block marked STI_FIRST is never linked to one marked STI_FIRST at the same RIP, i.e. to itself (the exit
     of its STI to its own start must reach `find`; linked, an event held would loop in generated code). Its other exits and every exit into it link as before.
2. **STMXCSR / VSTMXCSR** (FEX ab62c7e06, shim, abi, admission field, resident).
   - fexc: the plain STMXCSR m32 (0f ae /3, no 66/F2/F3) is translated, gated as every SSE form (`--sse-gated` cuts it: #UD
     with CR0.EM or without CR4.OSFXSR, #NM with CR0.TS, the resident's); VSTMXCSR m32 likewise with the AVX gating (VEX.L 1
     and VEX.vvvv other than 1111b stay #UD cuts); LDMXCSR and VLDMXCSR stay the resident's. The value is `FexcMxcsr`
     (JIT form in `fexc_sites.inc`): `ldr wD, [x28, #mxcsr]; mrs x0, fpsr; ldrb w1, [x28, #simd_flags]; orr; and w1, w0, #1;
     orr; ubfx w1, w0, #1, #4; orr .., lsl #2; ubfx w0, w0, #7, #1; bic w0, w0, wD, lsr #6; orr .., lsl #1`, stored by an
     ordinary access site (#PF, #GP/#SS non-canonical, #AC at CPL 3 with AC, data breakpoints, the page-crossing split).
   - The resident writes MXCSR (controls and the flags banked so far) into FEX's `CPUState::mxcsr` (`frame::MXCSR`, 0x3cc) at
     every `enter` and never reads it back; admission lets generated code load those 4 bytes (`frame::GUEST_STATE`).
   - No change at CPL: STMXCSR is not privileged and the field holds the vCPU's own MXCSR.

**Why it is exact.**
- *STI.* At CPL 0 STI cannot fault (CPL <= IOPL; no VME/PVI in 64-bit mode); IF = 1, and the shadow only when IF was 0 (SDM
  Vol.2 STI; Vol.3 7.8.1). Where the inline STI runs, nothing is pending (else `find` ran it) and its next instruction is in
  the same block, so the shadow's boundary is inside the block: an event arriving after the STI arms a stop taken at a
  later exit (chunk 15's argument, unchanged); one arriving before its pending-byte load, at the block's first word, stops
  there at once (`interrupted`; no block marked STI_FIRST is entered with a shadow, so `shadow_entry` never names it) and
  `find` runs the STI; one arriving between the first word and the load writes the byte, the STI exits to its own start, an
  instruction boundary at which no instruction ran, and `find` runs it there. Every entry into such a block outside `find`
  (a link, the dispatcher's L1, `sys_fast`'s direct entry) reaches the STI with the pending byte written at its chain's
  entry or by a doorbell since, so the worst case is that one exit. NMIs: taken by `find` at the STI's boundary when
  unblocked (they do not need IF), blocked in the STI shadow as before (the model's choice). MOV SS then STI: the shadow
  condition sends it to the resident, which keeps the MOV SS shadow over the STI and lets the STI replace it. TF: blocks
  never run with TF (steps are compiled without `--cpl0`, so a traced STI is decoded and the resident's). An instruction
  breakpoint at the STI: found by `find` before the lookup; with RF set the resident runs the STI. So every STI either runs
  as before (the resident's transaction at the same boundary) or inline exactly as chunk 15's mid-block STI does.
- *STMXCSR.* x86 stores MXCSR, flags included, and changes nothing else; a fault on the store leaves memory and MXCSR as
  they were (SDM Vol.2 STMXCSR). natemu's MXCSR at an instruction boundary inside a block is, by `sse::bank`, the bank's
  value at the block chain's entry OR `mxcsr_flags(FPSR | SIMD flag byte, that value)`: FPSR is cleared and the byte emptied
  at every `enter`, linked blocks keep accumulating both, x87 ops save and restore FPSR around themselves, and the resident's
  mid-block work (helpers, divisions, shadow misses, splits, doorbells) resumes with the captured FPSR and does not rewrite
  the page, while every change of MXCSR's controls (LDMXCSR, FXRSTOR, XRSTOR, #XM) is a resident transaction followed by an
  `enter`. `FexcMxcsr` computes exactly that value at the instruction (DE from IDC only without DAZ, as the bank), so the
  stored word equals what the resident's STMXCSR stored after banking. Gating comes first (a cut), then the store's own
  faults (its site), as the SDM orders them; with the store faulting nothing is written and the bank later ORs the same
  flags it would have anyway.

**Admission and code shape.** Host tests (with `FEXC`): `cpl0_translates_cli_and_sti_inline` (STI first: inline, marked,
its exit to its own start a link point; first but before HLT, a REP string, LOCK NEG or alone: the resident's; a CPL-3
request cuts it), `interrupt_flag_forms` (STI first admitted, also twice; first before a self-exit or alone refused),
`stmxcsr_is_translated` (STMXCSR with FS, with SIB, after an ADDSS, VSTMXCSR: translated and admitted with the code shape at
either CPL; LDMXCSR, VLDMXCSR, the SSE- and AVX-gated forms cut), `guest_state_fields_and_i1_words` and the abi layout test
with the new readable field; `test_slice1.py` (VSTMXCSR and STMXCSR translated, LDMXCSR/VLDMXCSR cut). Containment: the
STI's exit is to a RIP of the block's own bytes through the linker's usual checks; the MXCSR field is this vCPU's own
architectural state (a guest store into the page, the recorded deviation, changes what its STMXCSRs store until the next
entry, as for FCW's field: recorded in abi `GUEST_STATE_VA`); `FexcMxcsr` adds no word class the code shape did not know
(`mrs fpsr` was already admitted for the I5c checks).

**Tests.** `run_k2.py stifirst` (new, judged by the SDM and the delivery model, CPL 0, the program's own IDT, at block
sizes default and 1 where noted): `held-find` (a hook posts 0x40, then `sti; inc r9; inc r10`: taken once after `inc r9`),
`held-linked` (2,000 rounds; in round 1,001 the event is posted before a JMP already linked to the STI's block, which is
entered with it held: taken once after the next instruction, R9 = R10 + 1), `nmi-held` (0x40 and an NMI before the STI: the
NMI at the STI with IF = 0 in its frame, then 0x40 after `inc r9`), `mov-ss` (`mov ss; sti` with 0x40 held), `tf` (#DB after
each of six traced instructions from the STI on), `forge-pending` (1,000 rounds of a CPL-0 store of 1 into the
event-pending byte, then a linked JMP to the STI: every round completes), `cpl3-forge` (CPL 3 stores 1 into the pending
byte and 0 into IF's, takes 0x40 at CPL 3, SYSCALLs into a kernel side that starts with STI: IF = 1 after it and back at CPL
3), `kicks-jmp`, `kicks-ret`, `kicks-movss` (4,000,000 rounds `cli; inc [W1]; X; sti; inc [W2]` with X a JMP, a CALL to a RET,
or `mov eax, ss; mov ss, eax`, under 0x40 and NMIs posted at random times: no 0x40 with IF = 0 or right after the STI, no NMI
right after the STI (nor at it after MOV SS), W1 = W2, more than 100 of each taken); at the default size no STI is decoded
but under TF and after MOV SS with an event held. `run_k2.py stmxcsr` (new, judged by the SDM and by the resident's FXSAVE
of the same state, CPL 0 and 3, block sizes default and 1): `flags` (SQRTSS of -1, 1 + the smallest denormal, 1 / 0,
FLT_MAX * 2, FLT_MIN * FLT_MIN, 1 + 2^-30, each on a fresh MXCSR, the last by VSTMXCSR: IE, DE PE, ZE, OE PE, UE PE, PE;
IE, ZE, PE raised in three blocks with a CPUID between: all three), `controls` (LDMXCSR of every flag, of FTZ | RC 11 | DAZ,
of RC 01: stored as loaded; DAZ with a denormal: no flag; FTZ with FLT_MIN * 0.5: UE PE; ZM unmasked: PE through the SIMD
flag byte), `place` (odd and page-crossing destinations), `ac` (CPL 3 with CR0.AM and AC: #AC(0) at the STMXCSR, nothing
stored); no STMXCSR or VSTMXCSR decoded. The existing STMXCSR/VSTMXCSR fault cases (`run_slice1_faults.py`: read-only and
crossing destinations, non-canonical through RSP, FS, the CR0/CR4/XCR0 gating) and the event programs (`ev-sti-ret`, `-jmp`,
`ev-mov-ss*`: STIs that start their blocks) now run the new forms.

Negative controls (`nc/mutant-*.diff`, `nc/red-*.txt`; scratch builds, reverted): `find` never running the STI itself:
7 cases red (`held-find`, `held-linked`, `nmi-held`, `mov-ss`, the three kicks: the vCPU loops between the STI's exit and its
block until the watchdog); the linker allowed to link the block to itself: `forge-pending` red (stuck at round 2); the
shadow condition dropped: `kicks-movss` red (139 NMIs at the STI after MOV SS); the field not written at `enter`: 12 of 14
`stmxcsr` cases red; written without its flags: 12 red; `FexcMxcsr` without FPSR: 4 red (`flags` and `controls`, default size;
at size 1 every flag is banked by the exit before the STMXCSR's block, so the field path alone is exercised there), without
the SIMD flag byte: `controls` red (the unmasked case), with the flag field shifted by 1: 4 red. Not controls by
construction: the RF condition (it only matters when an event arrives between the block's entry and its STI's load, not
reproducible on demand), and `FexcMxcsr`'s DAZ term (with FEAT_AFP's FIZ an input flushed under DAZ does not set IDC, so
the term never fires on this host; it keeps the value equal to the bank's formula).

**Measured** (`camp/`, `camp2/`: `campaign25.sh`, `campaign25b.sh`, `summ25.py`; the `natemu` stub, cache off,
`mitigations=off nokaslr`, 2 GiB; base: HEAD's code with series58-channel-r1; change 1: STI first (series59-sti); change 2:
both changes (series60-stmxcsr); interleaved, the order rotating; three rounds of every cell, three more of the cells slower
by median; every run verified or reached and uncontended; median (min-max), seconds; CPU s the process's with its fexc
servers, mean):

| Benchmark | N | base | change 1 (STI first) | changes 1 and 2 (+ STMXCSR) | change 1 | change 2 | CPU s base / 1 / 1+2 | runs |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `locks` | 1 | 5.25 (5.05-5.33) | 3.80 (3.79-3.83) | 3.81 (3.79-3.85) | -27.7% | +0.2% | 8.0 / 6.6 / 6.6 | 6 each |
|  | 4 | 7.12 (7.03-7.19) | 7.44 (7.20-7.60) | 7.51 (7.34-7.59) | +4.5% | +1.0% | 31.9 / 33.0 / 33.4 | 6 each |
| `mem` | 1 | 9.65 (9.44-9.82) | 8.44 (8.41-8.54) | 8.49 (8.45-8.56) | -12.6% | +0.6% | 12.6 / 11.4 / 11.5 | 6 each |
|  | 4 | 3.10 (3.01-3.14) | 2.74 (2.71-2.79) | 2.77 (2.75-2.79) | -11.7% | +1.0% | 15.0 / 13.8 / 13.8 | 6 each |
| `luajit` | 1 | 5.39 (5.30-5.41) | 5.34 (5.31-5.39) | 4.09 (4.07-4.12) | -0.9% | -23.5% | 8.5 / 8.4 / 7.2 | 3 each |
|  | 4 | 1.68 (1.66-1.77) | 1.73 (1.71-1.79) | 1.46 (1.44-1.52) | +2.9% | -15.8% | 10.7 / 10.7 / 9.4 | 6 each |
| Installer, cache off | 1 | 5.45 (5.39-5.56) | 5.42 (5.32-5.47) | 5.33 (5.30-5.44) | -0.7% | -1.5% | 6.9 / 6.9 / 6.8 | 3 each |
| `hackbench` | 1 | 11.83 (11.51-12.01) | 11.73 (11.60-11.84) | 11.78 (11.63-11.84) | -0.9% | +0.4% | 14.7 / 14.5 / 14.5 | 6 each |
| `build` | 1 | 11.49 (11.32-11.69) | 11.44 (11.22-11.48) | 11.43 (11.36-11.46) | -0.5% | -0.0% | 16.0 / 15.9 / 15.9 | 6 each |
| `zstd` | 1 | 1.93 (1.92-1.94) | 1.92 (1.90-1.95) | 1.88 (1.88-1.94) | -0.2% | -2.0% | 4.9 / 4.8 / 4.8 | 3 each |
| `cray` | 1 | 0.42 (0.41-0.44) | 0.42 (0.36-0.43) | 0.43 (0.40-0.44) | +0.7% | +2.5% | 3.2 / 3.2 / 3.2 | 6 each |

Change 1: `locks` N = 1 -27.7% and `mem` N = 1 and 4 -12.6% and -11.7%, beyond their spreads; the installer, `build`,
`hackbench` and `zstd` medians 0.2-0.9% lower within their spreads. `luajit` N = 4 +2.9% with overlapping ranges. `locks` N
= 4 is +4.5% (7.12 to 7.44 s, ranges 7.03-7.19 and 7.20-7.60): the guest makes more futex calls, as in chunk 15. Perf
resident, two runs each, alternating (`meas/m5.sh`): guest SYSCALLs 31.5 and 29.8 M with the base, 34.7 and 34.5 M with
change 1 (the `sched_yield`s and boot are 3.1 M of each), resident-run STIs 6.9 M against 70-83 k, interrupts 107-113 k
against 76-89 k; the extra 3-5 M system calls (about 0.57 us each, chunk 22's fast path) are the wall difference's order.
Change 2: `luajit` N = 1 -23.5% (5.34 to 4.09 s, 0.31 us per VSTMXCSR) and N = 4 -15.8%, beyond their spreads; every other
cell within 2.5% by median with overlapping ranges. Perf resident (`meas/`), `luajit` N = 1: system instructions 4.99 M to
0.78 M, linker entries 5.41 M to 1.21 M, no VSTMXCSR left; `locks` N = 1 with change 1: resident-run STIs 6.37 M to 275 (the
slow path and the steps).

**Applies to.** Change 1: every Linux kernel path that enables interrupts right after a call returns (`local_irq_enable()`
following an out-of-line call: 561 of this kernel's 569 STIs follow a `call`), whatever the workload: 0.12-0.58 M resident
trips per run on every benchmark and the installer (table above), 6.4 M on `locks` (futex and scheduler paths) and 4.7 M on
`mem` (page faults, mmap); the condition is architectural (CPL 0, the plain one-byte STI first, its next instruction
translatable). Change 2: any code that reads MXCSR: glibc libm's rounding-mode guard (SET_RESTORE_ROUND, in `sin`, `cos` and
the other functions that need round-to-nearest: 36 VSTMXCSR sites in `libm.so.6` and the legacy STMXCSR forms in its
non-FMA variants), the C99 `fe*` functions (`fegetenv`, `feholdexcept`, `fetestexcept`, ...), `getcontext`/`swapcontext`,
and JITs or runtimes that save MXCSR around calls; measured here only on `luajit` (its benchmarks call `math.sin`/`cos`
through glibc 4 M times). The installer and the other benchmarks execute almost none (0-1 per run).

**Checks** (`suite25.sh`, `suite/`, the final tree, series60-stmxcsr, one VM runner at a time, each case's output deleted as
it ended): `cargo test` 111 + 2 ignored (host), abi 65, also with
`FEXC` set; the resident stack audit `ok` (gate bound 16,768 of 32,768 bytes); the Python unit tests 317 OK; fixtures 64
and seed 44 at both block sizes; the i7 corpus at CPL 0 69 at both; faults 7,912 at both; `run_k1.py` 336 runs, no
problem; `run_k2.py` all ok (104 lines, `stifirst` and `stmxcsr` included) and `stifirst`, `stmxcsr`, `sysfast`, `swapgs`
again (52 ok); `run_slice3_vm.py` `system`, `delivery`, `events`, `contain`, `smp` 0 failures; `run_x3.py` 54/54 and every
group, `run_x4.py` 33/28/6, `run_l4a.py`, `run_l4b.py`, `run_s4.py` all ok; `run_boot_smoke.py` 4/4; `run_r1.py r1a`, `r1b`
ok (negative controls flagged); FEX ASM 2,270: 1,898 pass, 48 precision deviations, 324 unsupported, no failure;
`test_fexc.py` and `test_slice1.py` 47 OK. `--check-reuse --code-shape` boots, cache off (`checkboot25.py`,
`check/`; the last one run by `suite25c.sh`, after the suite's runner hit its time limit): installer N = 1 (865,604 resident copies checked against the host) and N = 4 (259,338)
reached the dialog; `luajit`, `mem` and `locks` at N = 1 verified; no halt, no admission refusal.

**Independent review** (read-only, of the diff): no blocking defect; applied (`-r1`; compiler series61-chunk25-r1, FEX
unchanged at ab62c7e06 with this tree's shim, `obj/fexc` SHA-256 `c9062170...3906`):
1. The linker's exclusion states its invariant: no link from a block marked STI_FIRST to one marked STI_FIRST at the same RIP
   (one live block per RIP and context makes it the block itself).
2. Admission requires the PC table's first entry at guest offset 0, so an STI at the block's start is always seen and the
   block marked. Host test `cpl0_translates_cli_and_sti_inline` refuses an object whose offset-0 entries name the NOP
   after the STI; red without the requirement (admitted, unmarked).
3. The shim's STMXCSR rule tests the 66/F2/F3 prefixes itself, whichever two-byte map FEX files the form under;
   `stmxcsr_is_translated` checks that `66/f3/f2 0f ae 18` give no code. Not red without the test: FEX does not decode
   those forms (its group-15 table has no prefixed /3 memory entry), so they are cut as not decoded first.
4. New cases, each red under its mutation (`nc/mutant-r1-*.diff`, `nc/red-r1-*.txt`): `stifirst` `sysfast-held` (SYSCALL,
   a literal exit after a hook posts 0x40 at CPL 3 with IF = 0, goes through the fast path into a kernel side that starts
   with STI: 0x40 once after its next instruction; red when `enter` writes the event-pending byte as 0) and `reuse-copy`
   (frame F at two upper-half pages: the block run once through the first, then, with 0x40 held, reached at the second as
   an in-resident copy, reported by the driver: 0x40 once after its STI's next instruction; red, a hang, when the copy
   does not keep STI_FIRST; a template below 4 GiB is never copied, since FEX moves such RIPs with 32-bit `movz/movk`, which
   the relocator refuses); `stmxcsr` `link` (ZE raised, a JMP linked from round 2 on to the STMXCSR's block: 50 stores
   with ZE; red when `FexcMxcsr` drops FPSR) and `resumes` (ZE, then in the same block an x87 FSQRT of -1, a 128/64 DIV
   through the division helper, or a first load from a fresh page (a shadow miss, retried), then STMXCSR: ZE alone; red
   when the x87 ops do not restore FPSR, and when `resume` loses the captured FPSR). The weakened self-link rule removed
   entirely: `forge-pending` red, as before.
Rerun on the final tree (`review-r1/`): `run_k2.py stifirst,stmxcsr,sysfast,swapgs` 63 of 63 ok; `cargo test` 111 + 2
ignored, abi 65, with and without `FEXC`; `test_slice1.py` ok. No resident hot path changed but the two conditions (the
link test and the PC-table check), so the wall measurements stand.

**Not done.** LDMXCSR/VLDMXCSR stay the resident's (2.2-2.5 k per run). A block that starts with an STI is never linked to itself, so a
loop whose head is that STI takes the linker at every back edge: `run_k2.py cost`'s `sti` row (`1: sti; dec r8; jnz 1b`)
stays 0.20 us per iteration (0.23 before, the resident's STI), the other rows unchanged (`nop_sti` 0.01, CLI, SWAPGS, RDTSC
0.00, CPUID 0.24, `div` 0.13, `syscall` 0.51). Telling the STI's own exit from a back edge to the same RIP (both link points
target the block's start) would need admission to mark the link point by its place in the PC table; not built. A shadow step
after MOV SS of an STI that starts a block now asks the host for the step's decode (the kept decode at that RIP is a block),
a host round trip only in that rare case. `locks` N = 4 is slower (more guest contention, as after chunk 15).

### Measured (chunk 26: REP STOS in bulk inside a page, and MOV r64, CR2 inline at CPL 0, November 5; uncommitted on 625df3c, series64-chunk26-r1)

Evidence: `natemu-raw/repfault-20261105-01/` (README there). FEX: three commits on the fork clone's local branch `natemu-chunk26`
(from `natemu` ab62c7e06, not pushed): 5188b9fa0 (REP STOS in bulk, `FexcRepStos`; the bulk pass's last element ends the
instruction by a branch), 57323cdd3 (MOV r64, CR2: 0f 20 decoded with its ModRM, `FexcMovFromCROp`) and, after the review,
0430dc8a6 (0f 20's ModRM decoded register-only; `FexcMovFromCROp` checks its form). Final compiler series64-chunk26-r1 (below,
"Review round"). Compilers
series62-repstos (5188b9fa0, the shim without the CR2 rule) and series63-cr2 (57323cdd3, this tree's shim); natemu's
submodule pin is unchanged. 625df3c only added the suite runner: the code measured against is 3dd4fb5's.

**Measured first.** REP strings (chunk 24's sampler, `breakdown-20261105-01/prof/profcls.json`, and the I2 throughput rows of the
boot-features plan): REP-string blocks are 17% of `mem`'s generated-code samples (the kernel's `clear_page`, `rep stosq` of a
page: 18% of block samples), 3.7% of `hackbench`'s (`rep movsq` 2.0%, `rep stosq` 1.2% of block samples: the kernel's user
copies and page clears), 3.8% of `zstd`'s (`rep movsb`), 2.9% of the installer's, 2.1% of `build`'s, 1.1% of `luajit`'s; no
CMPS/SCAS block is among any cell's sampled blocks. In the VM a 4 KiB `rep stosq` takes 151-174 ns, `rep movsq` 166 ns, `rep
movsb` 1,049 ns: the pass loop runs at the speed of the same loop natively (`loops.c` on the host, ns per 4 KiB, a page in
L1 / a 256 MiB stream: today's stosq pass 178 / 176, a tight `str` loop 139 / 139, `stp` of two quadwords 75 / 83, `stp` of
two Q registers twice 33 / 82, `dc zva` 72 / 45; movsb's pass 1,147 / 1,169, a tight byte loop 1,076 / 1,098, 16 bytes a
round 74 / 139). So only wider stores pay (not removing the pass's re-entry): STOS in 16-byte stores about halves `mem`'s
0.83 s of REP time (about 0.4 s, 5% of 8.5 s), 0.04 s on `hackbench`; MOVS in 16-byte chunks would save about 0.05 s on
`hackbench` (0.5%), 0.04 s on `zstd` (2%), under 0.1 s elsewhere: under 3% on every workload, so MOVS (and CMPS/SCAS) are
not built. Page faults (chunk 24's tables, `mem` N = 1, window): 1.24 M faults, each a MOV from CR2 (1,242 k MOV from CR) and
an IRET (1,245 k) run by the resident at about 0.235 us a trip (0.29 s each, 3.4% of `mem`), the delivery 0.47 s, and 6.43 M
shadow installs at 0.31 us (the abort 0.97 s, the walk and install 1.06 s). MOV from CR2 elsewhere: `build` 83 k, installer 54 k,
`luajit` 26 k, `hackbench` 3 k. So MOV from CR2 inline (only `mem` above 3%, by its trips alone) is built; an IRET fast path
would keep its resident trip (the frame pops, the CPL change) and save part of it (about 0.1 s, 1.2% of `mem`, if half the
trip goes): not built; cheaper shadow installs: below.

**Built.**
1. **REP STOS in bulk** (FEX 5188b9fa0, shim `FexcRepStos`, fexc `--bulk`, host, admission).
   - fexc: with `--bulk`, a REP STOS in 64-bit addressing (no 0x67) first runs `FexcRepStos`, then I2's one-element pass. The op:
     `ldrb w0, [x28, #df]; tbnz w0, #7, S` (DF = 1: nothing), for words and wider `and x0, x11, #7; cbnz x0, S` (RDI not
     8-aligned: nothing), x1 = min(RCX / (16 / w), (4096 - (RDI & 4095)) / 16) without flags (`lsr`, `and`, `movz`, `sub`,
     `lsr`, `sub`, `and .., asr #63`, `add`), `cbz x1, S`, the element replicated to 64 bits in x2 (RAX itself for
     quadwords), then `L: stp xD, xD, [x11]; add x11, x11, #16; sub x7, x7, #16/w; sub x1, x1, #1; cbnz x1, L; S:`. The `stp` is
     a recorded store-pair site of 16 bytes. The pass after it: RCX = 0 ends the instruction (the existing `cbz x7`); else one
     element, then, new, `cbz x7` to the instruction's end when that was the last element, else the exit to its own RIP.
   - Host: `--bulk` on every translate request but steps, and not while `alignment_gated` (alignment checking or a data
     breakpoint armed, part of `Cpu::code_context`: such blocks never run in the other context); in the answer cache's gating
     key (`GATING` 11: fexc reads the flag only for such a REP STOS, so no other answer depends on it); an object holding a bulk
     loop from a request without the flag is refused.
   - Admission (`bulk_loop`, object validation): the one backward branch admitted is the `cbnz x1` of exactly these words for
     the width of the REP STOS holding them (F3, no F2 or 0x67; the guest bytes decide the width), every skip to the
     sequence's end, the `stp` the instruction's recorded store-pair site, no branch from outside into the sequence. So the
     loop runs at most 256 rounds (x1 is written by nothing else) and every store stays inside RDI's page: the code shape
     exempts that store from the split check.
2. **MOV r64, CR2 inline at CPL 0** (FEX 57323cdd3, shim, abi, admission, resident).
   - fexc: under `--cpl0` MOV r64, CR2 (0f 20 with ModRM mod 11 and reg 2, no prefix but a REX without R) is translated as a
     load of `frame::CR2` (FEX's `L1Mask`, 0xa8, which no generated code used) into ModRM.rm's register; every other control
     register, a REX.R (CR10), LOCK, 66, F2, F3 or another mod stays the resident's; at CPL 3 it is a system cut as before.
     FEX's decoder now reads the ModRM of 0f 20 (it decoded two bytes), register-only (review round: mod is ignored by the
     hardware, so a mod other than 11 is not decoded and stays the resident's).
   - The resident writes CR2 into that field at every `enter` (0 at CPL > 0: the guest-state alias is readable at CPL 3) and
     never reads it back. Admission lets a block load it only if the request was at CPL 0, with the code shape only inside such
     a MOV.

**Why it is exact.**
- *REP STOS.* The SDM's REP STOS is element by element: ES:[RDI] gets AL/AX/EAX/RAX, RDI steps by the size and DF, RCX
  counts down, and a fault, trap or interrupt between iterations leaves RCX/RDI at that boundary, RIP at the instruction and
  RF set (Vol.2 REP, STOS; Vol.3 RF). Grouping iterations, as the bulk part does for interrupts (below), is the SDM's
  fast-string operation, which it permits only under two conditions (Vol.1 7.3.9.3, Vol.3 11.2.4.1): IA32_MISC_ENABLE bit 0
  set, and WB or WC memory. Both are kept (review round): the bulk part runs only while the fast-string byte the resident
  writes from bit 0 at every entry is 1 (WRMSR is a resident transaction, an entry follows), and a write to WT or WP RAM, as
  to UC RAM, is never made through a shadow by a block: its instruction runs alone, one element (WB and WC run natively). The bulk part runs only with DF = 0 and stores the elements in order, each `stp` holding
  16 / w whole elements, RCX and RDI updated after each, so at every store the registers are those of the boundary before its
  first element, and every element before it is written. Each `stp` lies inside RDI's page, which the x86 elements it holds
  share: a page fault (not present, read-only, U/S, a reserved bit) or a canonical fault is the same for all of them as for its
  first, so a fault at the store is the first element's: the resident's site path (as for the element's `str`) reports #PF with
  CR2 the store's address (RDI), a write's error code, RIP the REP's, nothing of it written (the page's translation refuses the
  store, so no byte of it there is written). A shadow miss retries the store; nothing in the loop is in a register the retry loses (x0-x2 are in the
  captured frame). With w >= 2 RDI is 8-aligned, so each 8-byte half is single-copy atomic and holds whole naturally aligned
  elements (as x86 writes each); bytes are atomic anyway. Interrupts and stops: the loop is at most 256 rounds and has no exit;
  a stop or event arms at the next exit, which the pass reaches after it, so events are taken at iteration boundaries only
  (later by at most one page's stores, as a fast string groups them: Vol.1 7.3.9.3). Data breakpoints and alignment checking:
  bulk blocks never run with either (code context); TF: steps never get the bulk part, so each iteration traps. UC RAM: a
  store to a UC page aborts (never shadowed for a block) and the resident runs the instruction alone from the site's state, in
  a step: one element, then back (M2's rule, element by element); WT and WP RAM likewise for writes (review round: their pages
  are shadowed for reads only, `translate`). A device page: a pair site is refused as a REP string's
  element was (no completion record): the same stop. Code writes: a store into a page holding translations aborts (shadowed
  read-only); into the block's own source page the instruction is stepped from the store's state (one element, then the new
  bytes are compiled), into another page that page is invalidated first, as for the element's store. The new `cbz x7` after
  the element is the instruction ending with RCX = 0, where the exit to its own RIP would have ended it (`rep_exited`).
- *MOV r64, CR2.* At CPL 0 it never faults (#GP needs CPL > 0; #UD needs CR10+, LOCK or another register, all left to the
  resident), it writes the 64-bit register with CR2 (REX.W ignored in 64-bit mode) and its flags are undefined (Vol.2 MOV:
  Move to/from Control Registers): the resident's MOV left them unchanged, and so does the load. CR2 changes only in the
  resident (a #PF delivery, MOV to CR2), and every return from the resident into generated code after one is an `enter`
  (`sys_fast` and the linker end in it too), which writes the field, so the field equals CR2 wherever a CPL-0 block runs.
  Under TF the instruction is stepped and run by the resident as before.

**Admission and code shape.** Host tests (with `FEXC`): `bulk_rep_stos_only_when_asked` (the loop with `--bulk` for stosq,
stosw, stosb; none for 0x67, MOVS or other code; none without the flag; a bulk object refused for a request without it),
`cpl0_translates_mov_from_cr2` (RAX and R9 forms and REX.W translated; REX.R, CR3, 66 and CPL 3 the resident's; the field's
load refused in an object admitted as a CPL-3 request's); `bulk_rep_stos_loops_are_pinned` (fixtures `rep-stosq-bulk`,
`rep-stosb-bulk`: admitted with and without the shape; the count decremented by 2, the page room in 8-byte units, RDI stepped
by 32, the back edge to the `add`, the DF skip into the loop, the bytes of another width, the `stp` moved off its site: each
refused by object validation). Both host tests fail against HEAD's compiler (`nc/red-host-tests-old-compiler.txt`).
Containment: the loop is pinned word for word and bounded; it stores only through RDI, at EL0, like the element's store; the
CR2 field is the vCPU's own state, readable only by CPL-0 code (0 in the page while CPL 3 runs).

**Tests.** `run_k2.py repbulk` (new, judged by the SDM, natemu only, block sizes default and 1): `fill-w1/2/4/8` (offsets
0, 8, 3, 0xff8, 0xff0, 0x10, counts 0, 1, a chunk less one, a chunk, one more, 100, two pages and 3, DF = 1 twice, 0x67
once: RCX 0, RDI the end, exactly the string's bytes), `fault-unmapped` (REP STOSQ from D+0xf00, D+0x2000 not present: #PF
with CR2 D+0x2000, error 6, RCX 480, RDI D+0x2000, the 0x1100 bytes before written), `fault-first` (a read-only page's start:
nothing written), `fault-misaligned` (REP STOSB from D+0x1003 into a read-only page: RCX 4099 at D+0x2000), `uc` (a UC page:
written, every element alone), `smc-other`, `smc-own` (NOPs over code run before on another page, and over the code after the
REP on its page: the new code runs), `linked` (an odd-count clear_page called N and 2N times: linker entries equal), `tf`
(#DB after each of 5 iterations, RCX 4..0), `watch` (a write breakpoint on the second quadword: one #DB at the REP with RCX
62), `kicks` (100,000 rounds of 64 KiB under interrupts at random times: 597 of 653 taken at the REP, each with RCX + (RDI - BUF)/8
the count, the quadword below RDI this round's, the one at RDI the last's, RF set; none bad). `run_k2.py cr2` (new): `value`
(two #PFs, CR2 read by RBX and R9 in the handler), `nested` (a #PF inside the #PF handler: CR2 the outer's, then the
inner's), `mov-to-cr2` (MOV to CR2 then reads in the next block and after a JMP), `tf`, `cpl3` (the alias's field 0 at CPL 3,
MOV r64, CR2 #GP(0) there). The I2 corpus against QEMU (484 programs, both block sizes), its fault flow form, M2 (UC) and M3
(devices) also run the new forms (Checks).

Negative controls (`nc/mutant-*.diff`, `nc/red-*.txt`; scratch builds, reverted): RCX stepped before the store: 3 fault cases,
`uc` and `smc-own` red (RCX a chunk short at the fault); no DF test: `fill` red at every width (DF = 1 stored upwards); the page
room in 8-byte units: `fault-unmapped` and `fault-misaligned` red (a store straddling the page end, the fault at the earlier
boundary); bulk in steps too: `tf` and `uc` red (a page in one step); CR2's field not written at entry: `value`, `nested`,
`mov-to-cr2` red; written at every CPL: `cpl3` red (CPL 3 reads the kernel's CR2).

**Measured** (`camp/`: `campaign26.sh`, `summ26.py`; the `natemu` stub, cache off, `mitigations=off nokaslr`, 2 GiB,
bench-20261104-02's initramfs; base: HEAD's code with series61-chunk25-r1; change 1: REP STOS in bulk (series62-repstos);
change 2: both changes (series63-cr2); interleaved, the order rotating; three rounds of every cell, three more of the cells
slower by median and of `mem` N = 4; every run verified or reached and uncontended; median (min-max), seconds; CPU s the
process's with its fexc servers, mean):

| Benchmark | N | base | change 1 (REP STOS bulk) | changes 1 and 2 (+ CR2 inline) | change 1 | change 2 | CPU s base / 1 / 1+2 | runs |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `mem` | 1 | 8.46 (8.33-8.51) | 8.15 (8.15-8.23) | 7.83 (7.78-7.83) | -3.6% | -3.9% | 11.3 / 11.1 / 10.8 | 3 each |
|  | 4 | 2.75 (2.68-2.78) | 2.69 (2.62-2.74) | 2.58 (2.53-2.61) | -1.9% | -4.4% | 13.8 / 13.5 / 13.1 | 6 each |
| `hackbench` | 1 | 11.73 (11.51-11.74) | 11.64 (11.57-11.64) | 11.62 (11.61-11.65) | -0.8% | -0.2% | 14.4 / 14.4 / 14.4 | 3 each |
| `build` | 1 | 11.36 (11.27-11.41) | 11.34 (11.27-11.43) | 11.35 (11.27-11.43) | -0.2% | +0.1% | 15.8 / 15.8 / 15.9 | 6 each |
| Installer, cache off | 1 | 5.37 (5.29-5.47) | 5.31 (5.28-5.47) | 5.36 (5.29-5.41) | -1.1% | +0.9% | 6.8 / 6.8 / 6.8 | 6 each |
| `zstd` | 1 | 1.92 (1.89-1.96) | 1.90 (1.84-1.93) | 1.88 (1.84-1.93) | -1.3% | -0.8% | 4.8 / 4.8 / 4.8 | 6 each |
| `locks` | 1 | 3.81 (3.79-3.82) | 3.85 (3.79-3.88) | 3.83 (3.76-3.86) | +1.2% | -0.7% | 6.6 / 6.6 / 6.6 | 6 each |
| `luajit` | 1 | 4.10 (4.04-4.15) | 4.08 (4.07-4.11) | 4.08 (4.06-4.11) | -0.4% | -0.1% | 7.2 / 7.2 / 7.2 | 3 each |
| `cray` | 1 | 0.42 (0.41-0.43) | 0.43 (0.40-0.44) | 0.42 (0.39-0.43) | +3.2% | -1.3% | 3.2 / 3.2 / 3.2 | 6 each |

Change 1: `mem` N = 1 -3.6% (8.46 to 8.15 s, ranges apart) and N = 4 -1.9% (overlapping); every other cell within 1.3% by
median with overlapping ranges, but `cray` (+3.2%, 0.42 s, its ranges overlapping; it runs no REP STOS in its window). The 4
KiB rows of `run_k2.py cost` (`cost/`, N = 100,000, slope of N and 2N, median of 3): `rep stosq` 0.174 -> 0.067 us, `rep
stosb` 1.009 -> 0.061 us. Change 2: `mem` -3.9% and -4.4%, beyond the spreads (estimate 3.4%); every other cell within 1% by
median. Both: `mem` N = 1 8.46 -> 7.83 s (-7.4%), N = 4 2.75 -> 2.58 s (-6.2%).
Found on the way (`camp-v1/`, `diag/`): the first build of change 1 made `mem` 12% slower (linker entries 4.4 M -> 10.5 M, the
linker's time 0.33 -> 0.73 s with the perf resident): a pass's bulk part usually left the last element alone, whose exit to
the REP's own RIP with RCX = 0 is a boundary past the instruction (`rep_exited`), which the resident never links, so 6.09 M
passes entered the linker (a scratch count by the exiting block, `diag/scratch-linker.diff`). The pass now ends the
instruction by a branch after its last element (5188b9fa0), and `repbulk`'s `linked` case pins it.

**Applies to.** Change 1: every OS's page zeroing by REP STOS in 64-bit code: Linux's `clear_page` (`rep stosq`, and `rep stosb`
with ERMS) on every anonymous page fault, page-table page, `__GFP_ZERO` allocation and page-cache fill of a hole; Windows' and
the BSDs' page zeroing where it uses REP STOS; `memset`/`bzero` in kernels and firmware built to REP STOS (GCC and MSVC
inline it at -Os, glibc uses `rep stosb` for large sizes when ERMS is set); the condition is architectural (64-bit addressing,
DF = 0, RDI's alignment, the count, the page end; no alignment checking or data breakpoint). Measured here on `mem` (fault and
mmap churn); the others clear fewer pages. Change 2: every page-fault handler that reads CR2 at CPL 0 (Linux's `exc_page_fault`,
every 64-bit OS's #PF entry), so every page-fault-heavy workload: fork and exec, mmap/munmap churn, first touches, copy on write.

**Checks** (`suite26.sh`, `suite/`, the final tree, series63-cr2, one VM runner at a time, each case's output deleted as it
ended): `cargo test` 114 + 2 ignored (host), abi 65, also with `FEXC` set; the resident stack audit `ok`; the Python unit
tests 317 OK; fixtures 64 and seed 44 at both block sizes; the i7 corpus at CPL 0 at both; faults (`run_slice1_faults.py`) at
both; the I2 corpus against QEMU 484/484 at both block sizes and its fault flow form 4,356/4,356 at sizes default and 1 (1,271
faulting, inside REP strings among them); `run_m2.py` 112 cases, `run_m3.py` 0 failures; `run_k2.py` all ok (the new
`repbulk` and `cr2` included) and `stifirst`, `stmxcsr`, `sysfast`, `swapgs`, `repbulk`, `cr2` again; `run_slice3_vm.py`
`system`, `delivery`, `events`, `contain`, `smp` 0 failures; `run_x3.py`, `run_x4.py`, `run_l4a.py`, `run_l4b.py`, `run_s4.py`,
`run_boot_smoke.py`, `run_r1.py r1a`, `r1b` ok; FEX ASM 2,270: 1,898 pass, 48 precision deviations, 324 unsupported, no
failure; `test_fexc.py` and `test_slice1.py` 47 OK. `run_k1.py` failed once (`adv-pf-fetch@4`: "FEX frame changed"): the test
driver's frame check did not know CR2's field, which a CPL-0 entry after a #PF fills; with the field admitted there
(`program.rs` `frame_intact`, as for IA32_KERNEL_GS_BASE's) `run_k1.py` passes, and `cargo test` again (both ways).
`--check-reuse --code-shape` boots, cache off (`checkboot26.py`, `check/`): installer N = 1 (872,426 resident copies checked
against the host) and N = 4 reached the dialog; `mem` and `build` at N = 1 verified; no halt, no admission refusal.

**Review round** (independent review: no blocking defect; two SDM gaps fixed and hardening, `-r1`: FEX 0430dc8a6 on the
two commits above, compiler series64-chunk26-r1, `obj/fexc` SHA-256 `71c8349f...05ba6c`, `bin/r1`, patch
`chunk26-repfault-625df3c-r1.patch`):
1. Fast-string conditions (SDM Vol.1 7.3.9.3, Vol.3 11.2.4.1: grouping iterations needs IA32_MISC_ENABLE bit 0 and WB or WC
   memory; natemu lets the guest clear bit 0 and runs WT/WP RAM natively). The resident writes bit 0 into a flag byte of the
   guest-state page (`frame::FAST_STRINGS`, FEX's flags[23], which FEX never used) at every entry; `FexcRepStos` starts with
   `ldrb w0, [x28, #fast_strings]; cbz w0, S` (admission's template too; the byte is readable, never writable). A write to WT
   or WP RAM is handled as one to UC RAM: never through a block's shadow (its page shadowed for reads only), its instruction
   run alone (`translate`). No deviation recorded.
2. FEX 57323cdd3 decoded 0f 20 with a memory operand for mod other than 11 (a SIB and displacement: `0f 20 05` as seven bytes,
   so near a page end a fetch #PF where the SDM gives #GP; the hardware ignores mod): 0f 20 is now decoded register-only
   (`FLAGS_SF_MOD_REG_ONLY`), so such a form is not decoded by FEX and stays the resident's (the host's decode already took
   it as three bytes).
3. `FexcMovFromCROp` itself requires mod 11, reg 2, no REX.R and `--cpl0`; anything else is FEX's PermissionRestrictedOp
   (which admission refuses), whatever the allowlist let through.
4. `FexcRepStos` is a guest-memory op for the precision passes (`fexc_precision.h` `GuestMemOp`): no value is coalesced into a
   static register across it (it writes RCX's and RDI's itself), and the incoming flags stay live at it.
5. Tests (`run_k2.py`, all at block sizes default and 1 unless noted): `repbulk` `kicks-w1/w2/w4/w8` (every width under random
   interrupts: element and RF checks; with fast strings on no boundary taken inside a REP lies in a page's interior, 16 to 4079
   bytes in) and `kicks-w1/w8-misc-off` (bit 0 cleared: 2,969 of 2,998 and 452 of 455 there), `wt-wp`, `cpl3-us` (#PF error 7),
   `cpl3-rsvd` (error 0xf), `noncanonical` (#GP(0), RCX and RDI unchanged), `into-noncanonical` (the last canonical page's 512
   elements, then #GP(0) with RCX 1), `tso` (a second vCPU never sees a buffer element older than the flag stored after the
   REP STOSQ); `cr2` `mod-cpl3` (#GP(0) at `0f 20 05` ending a page, the next not mapped), `mod-cpl0` (`0f 20 15`: RBP = CR2,
   then the fetch #PF), `cr3`, `smp` (two vCPUs, 20,000 #PFs each, CR2 always the vCPU's own); admission's pinning test
   also refuses a forward branch from before the sequence into the loop, `cbz x1` removed, min made max (`bic`) and the
   fast-string test removed; `guest_state_fields_and_i1_words`: the fast-string byte loadable, not storable. Each new guard red
   under its mutation (`nc/mutant-r1-*.diff`, `.fex.diff`, `nc/red-r1-*.txt`): the fast-string test removed: both
   `misc-off` kicks red (no boundary mid-page); WT/WP writes native: `wt-wp` red (no element run alone); FEX's memory decode of
   0f 20 back: `mod-cpl3` red (a partial-decode stop at the page end instead of #GP) and `mod-cpl0` red (the fetch #PF before
   the MOV, RBP 0); the allowlist slipped to every control register with the dispatcher's check removed: `cr3` red (MOV RBX,
   CR3 gave CR2); the slip alone: the object refused (PermissionRestrictedOp's `br`), never a CR2 value. The precision barrier
   has no control by construction (no translation today coalesces across the op; it guards later edits).
6. Checks (`suite-r1/`, `python3 tools/run_suite.py --fexc series64-chunk26-r1`, now with `repbulk` and `cr2` in its k2 list):
   113/113 passed (`privilege` and every k2 group included), then `k2-repbulk` and `k2-cr2` 65 cases ok; `cargo test` 114 + 2
   ignored, abi 65, with and without `FEXC`; `--check-reuse --code-shape` boots on `bin/r1` (`check-r1/`): installer N = 1
   reached, `mem` and `build` N = 1 verified. Re-measured, as the pass gained two words (`camp-r1/`, three interleaved rounds,
   base against r1): `mem` N = 1 8.32 (8.28-8.39) -> 7.76 (7.71-7.77) s, -6.7%; N = 4 2.74 (2.68-2.79) -> 2.55 (2.54-2.55) s, -6.9%
   (the campaign above: -7.4% and -6.2%).

**Not done.** MOVS, CMPS, SCAS stay one element a pass (under 3% everywhere, above); STOS with DF = 1, misaligned words and
wider, and 0x67 stay one element a pass. An IRET fast path (1.25 M on `mem`, at most about 0.1 s) is not built. Shadow
installs (6.43 M on `mem`, 2.01 s) were not made cheaper: a fault-around that installs a missing page's neighbours would need,
to stay exact, only leaf entries already present with A set (writable only with D set and R/W), no A/D write, and the same
INVLPG/CR3 invalidation as today; whether it pays depends on how many of `mem`'s installs a neighbour's would have served,
which needs a census of the installs (user or kernel, the neighbours' A bits, refaults after INVLPG) first.

### Measured (chunk 28: three generated-code shapes, November 5; analysis on 6b2905b, series64-chunk26-r1, no code change kept)

**Question.** Of three costs every block pays, which is largest across workloads and can be made cheaper exactly: (a) the
page-crossing check before each store wider than a byte, (b) a SIMD load into a scratch register and then a 128-bit move to
the guest register, (c) the dispatcher's code for computed exits. Build the largest exact one only if it clearly pays.

**Method** (`natemu-raw/codeq-20261105-01/`, README there). Chunk 24's PC sampler (`scratch-prof28.diff`: the same diff, plus
the code-area offset of each dispatcher sample), built from HEAD and removed again (the default resident's loaded sections
byte-identical to HEAD's), one run per cell, `build`, `zstd`, `cray`, `luajit`, `hackbench` at N = 1, with series64-chunk26-r1;
`codeq28.py` classes each sampled native word (the sample's PC is the oldest instruction not yet retired). Two shares per shape,
of EL0 samples (blocks plus dispatcher): *held*, the samples on the shape's words; *words*, the shape's share of each block's
words times the block's samples (as if every word cost the same). Then two measurements: a ceiling for (a), a scratch
compiler with every split check removed (`scratch-nosplit.diff`, inexact, timing only; three interleaved rounds against base),
and `disp28.c`, a host microbenchmark of the computed-exit lookup on this M4.

| Share of EL0 samples, % (held / words) | `build` | `zstd` | `cray` | `luajit` | `hackbench` |
| --- | --- | --- | --- | --- | --- |
| EL0 samples in the window | 18,716 | 5,794 | 1,026 | 8,578 | 11,633 |
| (a) split check (`[add/sub x1;] add x0, A, #w-1; eor; tbnz #12`) | 4.4 / 4.7 | 5.9 / 7.9 | 0.1 / 0.1 | 7.0 / 11.7 | 11.9 / 12.4 |
| of which before a push (`str x, [x8, #-8]!`), % of (a)'s held | 66 | 18 | - | 46 | 83 |
| (b) SIMD load then `mov v.16b` | 0 / 0 | 0.2 / 0.2 | 19.2 / 4.8 | 1.2 / 0.6 | 0 / 0 |
| (c) dispatcher head and L1 entries (+ the exits' target check) | 15.0 (16.5) | 3.7 (4.6) | 0.6 | 5.4 (7.7) | 9.0 (12.8) |
| of the dispatcher's samples: L1 entry's first word / dispatcher's first word, % | 77 / 20 | 80 / 15 | - | 51 / 41 | 59 / 38 |

**Estimates** (share x chunk 24's generated-code seconds, `build` 6.14, `zstd` 1.73, `cray` 0.31, `luajit` 3.10, `hackbench`
5.35): (c) is the largest: `build` 1.01 s, `hackbench` 0.68, `luajit` 0.24, `zstd` 0.08 (about 2.0 s over the five); (a) by
words `build` 0.29, `zstd` 0.14, `luajit` 0.36, `hackbench` 0.66 (about 1.45 s); (b) `cray` 0.06 by its held samples (0.015 by
words), `luajit` 0.04, nothing elsewhere (about 0.1 s).

**Ceiling of (a)** (`ceil/`, every split check removed, three interleaved rounds, median (min-max) s; every run verified):

| Benchmark, N = 1 | base | no split checks | change |
| --- | --- | --- | --- |
| `build` | 11.51 (11.45-11.52) | 11.08 (11.03-11.12) | -3.7% |
| `zstd` | 1.91 (1.89-1.96) | 1.78 (1.77-1.79) | -6.7% |
| `luajit` | 4.12 (4.12-4.15) | 3.86 (3.83-3.90) | -6.5% |
| `hackbench` | 11.89 (11.86-11.94) | 12.02 (11.88-12.08) | +1.1% |

So the samples at `hackbench`'s push checks (12%) are not the checks' cost (removing them gains nothing there, the ranges
overlapping); removing every check gains 3.7-6.7% on the other three.

**Dispatcher microbenchmark** (`disp28.txt`; K blocks of ten instructions, each ending in a computed exit; ns per exit over
the 0.56 ns of a block reached by a direct branch; K = 16 / 256 / 1,024):
- the next block predictable (each exit site always goes to the same block): today's code L1 0.95 / 1.03 / 2.38; a data table
  (`ldp` of tag and target, `br`) 0.70 / 0.71 / 0.74; a one-entry inline cache per exit site (its RIP's compare, a direct `b`) 0;
- the next block random: code L1 4.91 / 5.36 / 5.40; data table 5.89 / 6.24 / 6.26; inline cache (missing to today's path)
  4.83 / 5.60 / 5.97.

**Nothing built.**
- (c), the largest, is mostly the indirect branch itself: the samples sit on the first word reached after the dispatcher's
  `b` and its `br`, not on the tag compare (under 1% of the dispatcher's samples), so a shorter compare saves nothing. A
  data table saves 0.25-1.6 ns on a predictable exit (the code L1's instruction-cache footprint at 1,024 targets) and costs
  about 1 ns on an unpredictable one (its load sits before the branch resolves), and it needs an EL0-readable page in the
  private window: a guest load at that non-canonical address would read the table instead of #GP, a new deviation like the
  guest-state alias's (owner decision). An inline cache removes a monomorphic site's cost entirely but is linker-scale work:
  a multi-word patch per computed exit, filled from a resident trip, unlinked with the direct links, and admitted. Neither is
  one time-boxed experiment, and which pays depends on the mix of predictable and unpredictable exits, which nothing measures yet.
- (a) has no cheaper exact form for a general store: three words is the shortest flag-free test of "crosses 4 KiB". Where the
  check is provably redundant, after an aligned SSE/AVX store's alignment check on the same address (`alignsplit28.py`), it
  holds at most 0.15% of EL0 samples (`luajit`; 0.03% or less elsewhere). The exact elision the ceiling points to is an 8-aligned stack: an 8-byte push, or an RSP-relative store whose
  offset is a multiple of its width, cannot cross a page while RSP is 8-aligned; one guard per block (`and x0, x8, #7; cbnz`)
  could replace the checks of every such store after it while RSP changes only by multiples of 8, the guard's stub stepping
  the instruction otherwise (a performance cliff for misaligned stacks, exact). It needs a new stub kind, its resident path,
  and an admission proof over RSP's writes; by the pushes' share of the split samples, at most about 2.4% of `build`, 3% of
  `luajit`, 1% of `zstd` (other RSP-relative stores not separated): not built in this experiment.
- (b) is not about upper bits: the load goes to a scratch register because after a synchronous abort a load's destination
  is UNKNOWN (DDI0487 D1.4.1.4; `fexc_precision.h` `LoadsInto`), so a direct load could corrupt the guest register at a #PF.
  It cannot be dropped exactly, and it is at most 0.1 s anyway.

**Applies to.** Nothing changed. The findings apply to every workload: computed exits are every `ret`, indirect `call` and
`jmp` (switch tables, virtual calls, interpreters, PLT stubs); split checks every store wider than a byte, pushes and calls
included; the load-then-move every SIMD load.

**Next, if wanted.** For (c): a scratch count of computed exits per site (how many targets each site has) decides between an
inline cache (monomorphic sites) and nothing; the data table needs the owner's decision on the readable page first. For (a):
the aligned-stack guard, measured against the ceiling's 3.7% on `build`.

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
(Built in chunk 10, by a hint table by frame instead of the reverse map: 854 k requests avoided at N = 1, -20% wall.)

**Shared in-memory cache and compiler pool.** *Hypothesis:* at N > 1 vCPUs compile the same blocks again; the cache
resets wholesale at 65,536 entries (`execution.rs:842-846`). *Mechanism:* one cache per VM (sharded locks), keyed on
`supported` too, holding the admitted form so a hit skips re-admission (every object still admitted for its RIP);
second-chance eviction instead of the reset; the `fexc --serve` children (536) become a pool. *Risk:* the tamper hook
(862) must act on a copy. *Decides:* duplicate-key share, admission time.
(Built in chunk 13 as one sharded map of fexc's raw answers, each still relocated and admitted per vCPU, one server per vCPU
kept: installer N = 4 -28%, `build` N = 4 -28%.)

**Translating ahead of use.** After admission, queue the block's link targets (1339) on pages this request fetched (no
host walk of guest tables) to idle pool servers; answers enter the shared cache keyed by bytes read, used only if the
resident's bytes match (695). Worst case a wasted compile; bounded queue; host cores are the risk. *Decides:* share of
requests at a predecessor's target on a fetched page, then the speculative hit rate. (Built in chunk 16, off by default:
31% of compiles served ahead and half the rest waited for, but no wall gain beyond the spread.)

**Placing successors in the resident ahead of use.** A translate reply may carry admitted successors on pages the
resident marked for this request; it places each under the block's race rule (`kept`: a write since the mark kills
it). A first visit then costs a resident lookup (about 2 us), not a round trip. *Decides:* exits per first-time block.

**Address-space switches.** *Hypothesis:* CR3 loads and the shadow misses after them weigh heavily in a fork- and
exec-heavy installer. *Mechanism:* keep a few data-shadow sets tagged by CR3 root, chosen at a load instead of reset
(no PCID offered to the guest). A CR3 load without PCID flushes non-global entries, so a kept shadow must equal a fresh
walk: valid only while no page-table page on its walk was written, tracked as K1 tracks code pages (an A bit cleared
included). *Risk:* TLB semantics; new SMP cases in `run_k1.py`. *Decides:* CR3-load and shadow-miss ticks.
(Chunk 14 built the global part: the supervisor upper half's global shadows survive CR3 loads, 94% of `hackbench`'s misses;
per-CR3 sets of the non-global part remain a candidate, with PCID.)

**Per-block overhead.** As the profile shows: fexc's fixed cost (Config rebuilt per request since P3's leak fix; the JSON report the host dropped for objects: removed in chunk 2; argument parsing and the feature table
per request, chunk 2's profile; Config, arguments, feature table and reply copies: removed in chunk 20); IPC by pipes and `poll` (564-611), a
shared-memory ring with a native wait only if material (chunk 21: a shared channel with both sides spinning, installer -17%); staging, placement; admission's quadratic site scans.

**Resident entry cost.** About 0.36 us fixed per entry, 0.167 us the cheapest (plan, P2 row). Ticks in `bank`
(`runtime/src/execution.rs:1502`), `sse::bank` (`sse.rs:28`), `block_at`, `enter` (715-757), `tables::select`
(`tables.rs:67`); then copy only what changed, skip `tables::select` when CPL and root are unchanged, link exits into
kept system records. (Chunk 9: the copies cut, 0.19 to 0.044 us of `enter` per entry; `tables::select` already
skipped; copying only what changed dropped, it would put exact fault state at risk for about 10 ns. Chunk 12: system instructions' own costs cut and the L1 switch removed; a direct link into a kept system record measured
not worth it.)

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

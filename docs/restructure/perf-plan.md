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
requests at a predecessor's target on a fetched page, then the speculative hit rate.

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

**Per-block overhead.** As the profile shows: fexc's fixed cost (Config rebuilt per request since P3's leak fix,
`fexc.cpp:463`; the JSON report the host dropped for objects: removed in chunk 2; argument parsing and the feature table
per request, chunk 2's profile); IPC by pipes and `poll` (564-611), a
shared-memory ring with a native wait only if material; staging, placement; admission's quadratic site scans.

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

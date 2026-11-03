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
   180 k cache off, 9.5 k warm). A KASLR kernel compiles 61.5 k blocks of which 121 repeat bytes: its relocated text
   differs in bytes, so byte-keyed reuse does not cover it (4.70 s against 2.62 s, +2.1 s of compiles).
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

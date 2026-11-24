# Where translated-code time goes: a sampling profiler as framework cases (plan)

Status: plan, November 23, 2026, on `m2-peripherals` at 670fab60. Accepted with fixes after two independent review
rounds (section 8), the fixes taken in.
Measurement only: no translator change. Whether the sampler ships in the normal resident behind a run-time switch, or
stays a measurement build, is the owner's decision (section 6); chunks 0-2 are the same either way.

## 1. Why, and what it must decide

Translated code is now most of natemu's busy time on compile-heavy work: 305 of 418 busy vCPU-s in the i386
`make -j4`, 26.6 of 62.9 s in the i386 configure, 22.7 of 59.5 s in the 64-bit configure (bench case
`io5-solo32-profile`, M4; the [pfchain](m2-pfchain-findings.md) and [pic32](m2-pic32-findings.md) findings). Nothing
measures where that time goes. The [guest-code review](guest-code-review-20261114.md) ranked candidates from a November 5
sample of the bench `build` (scratch scripts kept in `natemu-raw`: an experiment), and the code has changed since:

| Since the review | What it changed |
| --- | --- |
| Dispatcher tables as data ([layout page](translated-code-layout.md) section 2) | a computed exit is a slot that loads and compares; returns about 9 ns dearer than a linked jump (that page) |
| Extended blocks (`--ext-conds 4`), hot/cold layout | fewer block boundaries; dense hot copies |
| One stack check per run of stores at an aligned base (`--push-run`) | the review's "aligned-stack split-check guard", built (any aligned base x2 and up) |
| Narrow compares (`--narrow-cmp`) | fewer flag words on 8/16-bit compares |
| Page-crossing stores through shared probes (M2 task 4) | no trap on a crossing store; two-word stubs |

The measurements below, each from a framework case, decide:

- **D1, computed exits.** Do the dispatcher slot and the words it lands on still hold 8% or more of EL0 samples? How
  much of that is returns and how much indirect `jmp`/`call`? Are those sites mostly monomorphic (an inline cache per
  site), and are returns predictable by a return stack (call/return pairing)?
- **D2, glue against FEX's body.** On the hot blocks, what share of samples falls on natemu's words (split checks and
  probes, stack checks, load-then-move, exits and trampolines, x87 run tests) against FEX's own body and its flag words?
- **D3, cold state after a resident trip.** How much guest-code time does one return from the resident (EL1) to
  translated code (EL0) cost beyond the gate itself?
- **D4, by symbol.** Which binaries and functions hold the time (REP string bodies, glibc's AVX2 string functions, the
  kernel's copy routines)?

## 2. What exists, and why it cannot be used as it is

The `profile` resident feature (the November 15-19 experiment series, d6ab555f): at an IRQ taken in generated code,
`study::prof::sample` classes the interrupted PC (code area head, step slot, block: user or kernel, part, distance from
entry, A64 word class, hot or cold copy) and counts blocks in a table of 2^18 entries (frame, page offset, user; RIP; the
first 24 guest bytes). The host's `NATEMU_SAMPLE_US=N` rings every vCPU's host doorbell every N us; the counters and the
80 blocks hit most go into the K1 stats JSON in the performance log, with native code only for blocks not flushed since.

The review found it unfit as an instrument:

1. **Every sample stops the vCPU.** The ring is the host doorbell (`vm.rs` `ring`), the same SPI real I/O uses, so the
   resident must treat it as an event: `interrupted` (`execution.rs`) stops at once in the dispatcher or at a block's first
   word and runs `dispatch`; inside a block it arms a stop (every exit word patched, `cache::arm`), traps at the exit,
   dispatches, disarms and re-enters. A halted vCPU is woken from `wfi` by each ring, which also shifts halt-poll
   adaptation. The profile perturbs exits, links and halts, the things D1 and D3 measure.
2. **`sample` runs for every IRQ from EL0** (LAPIC timer, kick SGI, real I/O), so samples are not periodic.
3. **The `RESUMED` filter is biased.** It drops samples whose PC equals the last resume PC: rings pending while the
   resident ran, but also genuine samples on entry words, which D1 needs.
4. **One dump at the end.** The `io5` boot runs four steps (64-bit and i386 configure and make); per-step results are
   impossible.
5. **Native words are lost to flushes**: a block's words are dumped only if no flush happened since its last sample;
   `build` fills the code area several times and CPython about 30.
6. **No room**: the 18 MiB study area (`PCID_STUDY_BYTES`) holds the 16 MiB table; growing it moves other areas in
   every resident.

## 3. The design

**A sample-only interrupt.** The host gets a second SPI per vCPU, the *sample bell*: `spi + MAX_VCPUS + v`, after the
whole doorbell range (`vm.rs` checks for `MAX_VCPUS` SPIs today: it then requires `2 * MAX_VCPUS`, and configures the
bells as it does the doorbells: group 1, edge, priority 0x80, enabled, each routed in `route(v)` after its vCPU exists,
the IROUTER quirk). The resident knows no SPI number today, so the host passes the bell's INTID in the shared record.
The resident's IRQ path tests the INTID first: for the sample bell it records, ends the interrupt and resumes generated
code from the frame (as the spurious-INTID path does), with no bank, no stop, no arm. A stop the host still wants is armed
again on resume; `resume` finds the block by `block_at(pc)` and halts `BAD_STOP` when none holds it, which a PC inside a
shared probe (code area head, not a block) would do, so the bell's resume maps a probe PC to its store's block through
x0/d0 first, as `interrupted` already does. (The spurious-INTID path has the same latent fault today; it is fixed in the
same change, with a case.) A pending doorbell is not swallowed: each INTID is acknowledged on its own, and a pending
doorbell is taken at the `eret`. Only the sample bell samples; other IRQs are handled as today and are not sampled.
Cost per sample: the IRQ entry, two ICC accesses that trap under HVF's GIC (about 1.2 us each,
[io-benchmarks](io-benchmarks.md)) and the record: about 3 us (estimate), measured by chunk 0's overhead case.

**Halted vCPUs are sampled by the host.** A bell would end a halt before `park` sees it (the poll spin in `lapic::wait`
returns on any pending IRQ, `wfi` ends on it, and `park` then changes its halt-poll input). So the host does not ring a
vCPU whose `Events::halted` is set (it already reads that word for `idle`) and records a halted sample itself. For the
race in which a vCPU halts just after the host looked, the resident's waits (`lapic::wait`'s spin and sleep, the INIT and
mailbox waits) and `acknowledge` take a bell, count it (`bells_in_wait`) and go on waiting, without counting a wake or
changing the poll input.

**Every sample is placed, not dropped.** The host writes its ring time (CNTVCT, the same clock: the vCPUs' virtual offset is
0, `vm.rs`) into the vCPU's shared word before it rings. `resume_exception` stores its own CNTVCT (under the feature, before
`mov sp`: scratch registers only, no push or call, as its contract says). A sample whose ring time is before the last resume
was pending while the vCPU was outside generated code: it is recorded as "resident or host", not discarded; every other
sample is an EL0 sample at its PC. So the profile covers the whole vCPU: EL0, resident or host, halted. A ring that
lands between that store and the `eret` (about 50 loads) is placed at EL0 on the resume PC: a window of tens of
nanoseconds, stated in the findings. Rings that merge on the edge-triggered SPI are lost; the host counts its rings and
the report compares them with the samples.

**A ring of samples, drained by the host.** Each sample is a 32-byte record: CNTVCT; kind (EL0, resident or host, halted);
for EL0 the code-area PC and, when it is in a block, the block's RIP, source frame and code context, the native offset in
the block's whole code (through `native_offset` for a hot copy; for a probe, charged to the store's block found from x0/d0
as `interrupted` already does), the cache's flush count, and the ticks since the last resume; for a dispatcher slot the
target RIP (x0) and whether the bytes before it are a call (D1's return share; section 4, chunk 1). The ring is 1 MiB per
vCPU (32,768 samples, 8 s at 4 kHz) in the 2 MiB left free in the study area; the resident publishes its write index
with release ordering, the host reads it with acquire, drains every 250 ms (about 1,000 records) into `NAME.samples`, and
a lost-sample count says when it fell behind. The 16 MiB block table is no longer needed for
attribution and is left out of sampling builds.

**Native code from the host, not from the code area.** While sampling, the host also writes each admitted answer it
serves (object bytes with its site records, keyed by RIP, frame, context and a hash of the bytes; deduplicated) to
`NAME.objects`. Offline, a sample's (RIP, frame, context, native offset) finds its object and word, whatever was flushed
since, and the object's own site records give the word's role exactly where admission knows it (split checks, probes, stack
and alignment checks, exits); patterns cover the rest (load-then-move, flag words, x87 run tests). Templates and instances
share one object (they differ only in relocated values). Objects are written for the whole run, whenever sampling
starts, and the host-written pool's templates (`POOL_VA`) are written as the host fills it, so a block placed from the pool
has its object too. The report maps an object's word offsets onto placed code as placement does: the block's `start()`
against its `code`, `ENTRY_OFFSET`, and FEX's stripped tail; a unit test checks the mapping on a placed block.

**Time series and phases fall out of the ring.** Samples carry time, so the report slices them by step markers (the
`io5` steps print theirs on the serial console, which the runner timestamps with the host's wall clock; the natemu
process records (`mach_absolute_time`, wall clock) pairs at start and every drain, so a marker maps onto CNTVCT) or by
fixed windows. The perf buckets'
existing periodic snapshot (`snap` lines) gives the bucket time series beside them.

**Start and stop.** The host rings only while sampling is on: from the start (`--profile FILE`), toggled by a signal
while the VM runs (the way SIGINFO already prints vCPU state), or by the guest through the control port below. The
resident needs no switch.

**The guest's control port.** A test and profiling device, off by default like the `isa-debug-exit` device it sits
beside ([`debug_exit.rs`](../../crates/host/src/debug_exit.rs), ports F4H-F7H): registered at F8H-FBH only in a run that
samples. It is byte-oriented, because the guest's simplest writer, `/dev/port`, writes one byte per port in sequence:

| Port | Write | Read |
| --- | --- | --- |
| F8H | 1 starts sampling, 2 stops it; other values are ignored and counted | 1 while sampling, else 0 |
| F9H | a mark: the byte is its number | 0 |
| FAH-FBH | reserved: ignored and counted | 0 |

- A write is an ordinary port exit to the host: natemu has no port device in the resident (`ports.rs` `local`), so
  every IN/OUT reaches the host (after the TSS I/O-bitmap check at CPL > IOPL), a few microseconds, once per toggle.
  Start and stop set an `Arc<AtomicBool>` the ringing thread reads, and bump a sequence number; the ringing thread
  re-reads the flag before each vCPU's ring and logs, with every pass, the sequence number it rang under, so a pass in
  flight across a stop is visible. The device is registered beside debug-exit in both prepare paths (`linux.rs`), before
  the registry is frozen. A mark writes a record (vCPU, the host's `mach_absolute_time` at the exit, the number) into
  `NAME.samples` in time order with the samples, and, in a run with the performance log, a `snap` line of the perf
  buckets, so buckets split by phase too.
- `--profile-start guest` starts a run with sampling off until the guest's first start; the default is on from the
  start, so the port only narrows a run.
- The guest learns the device is there from its kernel command line (`natemu.profile=1`, added by the runner in sampling
  runs only: `tools/bench/run.py` and `tests/io/run_io5.py` build the command line). A probe would also work by default
  (F8H reads 0 or 1 when present, FFH when absent, as `debug_exit.rs`'s own test asserts), but under
  `--strict-unassigned` (some system and SMP tests) an unassigned port stops the run, so the scripts do not probe.
  Sampling boots thus differ from the `-profile` boots by that one parameter, which Linux keeps for a module named
  `natemu` and otherwise ignores.
- From a root shell: `printf '\001' | dd of=/dev/port bs=1 seek=248 count=1 conv=notrunc` starts, `\002` stops, and a
  mark is the same at `seek=249` with its number as the byte (`conv=notrunc`: `dd` otherwise tries to truncate at the
  seek). `write_port` in the kernel's `drivers/char/mem.c` writes one byte per port from the file offset with `outb` and
  needs CAP_SYS_RAWIO (from the source as known; chunk 0's case shows it on 6.12). The Debian 13 kernel config has
  `CONFIG_DEVPORT=y` (read from the `debian-kernel-deb` resource's `/boot/config`), and its lockdown, which would refuse
  `/dev/port`, applies only under EFI Secure Boot (`CONFIG_LOCK_DOWN_IN_EFI_SECURE_BOOT=y`; none today, a limit for M3's
  UEFI boots with Secure Boot on). The bench image's busybox `sh` and `dd` with devtmpfs serve; chunk 0's case checks that
  `/dev/port` exists in both guests. `sampler_check.c` writes the port with `ioperm` as root.
- F8H-FBH: nothing in natemu registers them (`register_ports` refuses overlaps), and they lie in the legacy coprocessor
  range next to the debug-exit ports. That Linux never touches them is checked, not assumed: the device counts every
  access, and a sampling boot whose scripts never write it must count zero.
- The bench and `io5` guest scripts mark their steps and start and stop around each benchmark window and step when the
  command line says the device is there. This is the same guest-script change as chunk 1's `_text` line: one change,
  one re-run of the cases that use those scripts.

## 4. Chunks

### Chunk 0: the sampler, its cases and its validity

- Resident and host as in section 3, under the `profile` feature (until section 6 is decided). The default and `perf`
  residents must not change: built at the parent and at this commit, their loaded sections compared.
- **Artifact** `profile-runtime` (`testkit/artifacts.py`, beside `perf_runtime`): `--features perf,profile` (the two touch
  separate areas and compose). Bucket times from a sampling run are reported but never used for a decision: buckets come
  from the unperturbed `-profile` cases.
- **Options:** `tools/bench/run.py --sample-us N` and `tests/io/run_io5.py --sample-us N` (each implies the performance
  log, where the stats land). The report gives the measured samples per second, not the requested rate.
- **Report** `tests/bench/sample_report.py` (refuses to run outside the framework): per case, per vCPU and summed, per
  phase: samples by kind, EL0 samples by place and by role, user and kernel, the top 50 blocks and symbols, the cumulative
  curve, lost samples. Writes `NAME.report.json` and a Markdown table. Unit tests on synthetic
  rings and objects (`tests/bench/test_sample_report.py`, run by the `unit` family).
- **Cases** `guest-<b>-sample` at 1 vCPU for `build`, `hackbench`, `luajit`, `sqlite`, `zstd`, `startup32` and
  `scimark32-sse2`; `guest-build-sample-4` at `--vcpus 4`; `io5-solo32-sample` (its four steps as phases). Placed with the
  `-profile` cases; their comments say they are profiles, not timings.
- **Validity case** `guest-sampler-check-sample`, on a static program added to the bench image
  (`tools/bench/image/src/sampler_check.c`), in four timed phases; its judge requires: (a) a one-block ALU loop: at least
  95% of EL0 samples on that block (body and its linked back-edge exit both count); (b) a loop calling a small function
  through a pointer: samples on the slot and the callee's entry, the slot samples marked indirect call, not return;
  (c) a loop storing 8 bytes across a page: samples in the probe range charged to the loop's block; (d) skid: a loop of
  dependent loads that miss the cache between cheap words; the judge records where the samples land around the long
  load (on it, or the next words) and the report applies that allowance to per-word roles. If (a) fails twice, stop: the
  plan goes back to review.
- **Overhead case** `guest-sampler-cost`: the bench `build` and `hackbench` at 1 vCPU, and `build` at 4, with the
  `profile-runtime` sampling off and at the chosen rate, alternating, three rounds, one machine. Accepted when wall
  differs by no more than the rounds' spread plus 2%, and the guest bucket's share by no more than 2 points. Start at
  250 us (4 kHz); halve the rate if it fails.
  Measured (case `guest-sampler-cost`, M2 Max, November 23): wall within its bound at 250, 500 and 1000 us; `build`'s
  guest share drops 1.0-1.9 points at every rate (hackbench under 0.3), and as much with the objects file off (case
  `guest-sampler-cost-no-objects`), so the drop is not the bells' per-sample cost or the objects writes; its cause is not
  measured (host threads beside a compile-heavy cold run is the guess). The first bound of 1 point was raised to 2 (the
  orchestrator's call), 250 us kept, and every profile from these cases states that `build`-like runs read 1-2 points
  less guest time than an unsampled run.
- **The control port:** the device in `crates/host` with unit tests (each command, the ignored values, the counts, a
  mark's record), and a phase of the validity case: the program runs a loop with sampling on, stops it, runs the loop
  for a fixed time, starts it again and runs the loop again, marking each phase. The judge places samples by ring time
  against the stop's and start's flag stores (from the ringing thread's sequence log), allows the one pass in flight at
  each, and requires no other ring in the stopped window, EL0 samples on the loop in both running windows (so a sampler
  that never starts again fails), the marks in order, and the device's count of stray accesses at zero.
- **One bench image change** carries both new programs (`sampler_check.c` here, `coldtrip.c` of chunk 3): every case on
  the image is re-run once, and any recorded outputs re-recorded.

### Chunk 1: roles, returns and symbols (report side, little resident work)

- **Roles:** the object's site records first, patterns for the rest, as section 3. A case translates the guest-code
  review's idiom corpus, 64-bit and 32-bit forms, with `{fexc}` and checks every word gets the role its idiom says, so
  the classifier fails loudly when fexc's output changes.
- **Returns against indirect jumps and calls:** a slot sample's target RIP is a return when the bytes before it end a
  `call` (`E8 rel32`, or `FF /2` in its register and memory forms, REX, SIB and disp32 included). The resident reads up
  to 15 bytes before the target through the target block's `source_gpa` with `guest_ram().range`, as `sample` reads guest
  bytes today, never through a guest page walk (the fetch walk sets A bits and can fault). When the bytes before the target
  lie on another page, or the target has no block (an L1 miss), the sample is "unknown" and counted. The report checks
  the split against the census mix of chunk 2.
- **Symbols:** kernel samples by `System.map` from the `debian-kernel-deb` resource, with the KASLR slide recovered from
  the guest: the `io5` and bench guest scripts print `_text` from `/proc/kallsyms` once at start in sampling runs (as root;
  if `kptr_restrict` hides it, the sampling cases boot with `nokaslr` and say so). This changes the `io5` guest script, so
  the `io5` cases are re-run once with it. User samples by matching their objects'
  guest bytes against the `.text` of the image's binaries at the same page offset (bench image); for `io5`, only if its
  binaries can be read from the io5 inputs, checked here, else user results stop at block level.
  As built (November 23): the `debian-kernel-deb` package's System.map is a one-line stub (the real one is in the -dbg
  package), so kernel symbols come from the kallsyms tables of the bzImage natemu boots (`{kernel}`, the same file io5
  boots), decompressed and read once per kernel into the framework's `kernel-symbols` input (`{kernel_symbols}`; its zstd
  payload needs Homebrew's `zstd`, a README test prerequisite, or Python 3.14). The report gives every role share raw and
  under a one-word skid correction (each sample counted on the next word executed), since the validity case's chase phase
  put most of a slow load's samples on the word before it; read section 7's FEX-body row off both. io5's binaries are installed in the guest's disk during
  the run, which the inputs do not hold: io5 user samples stay at block level. Debian's binaries are stripped, so user
  samples name a function only where `.dynsym` (or a bench program's own `.symtab`) has one; libc's AVX2/EVEX string
  functions have no names and are counted by proxy (libc blocks holding a VEX.256 or EVEX instruction). Roles:
  `tests/bench/sample_roles.py`, its corpus case `sample-roles` (tests/static).

### Chunk 2: the computed-exit census, under QEMU

The sampler sees where a return lands, not the polymorphism of its site. The census counts the program itself under
QEMU, on the same guest and programs:

- **Artifact** `qemu-plugins`: QEMU 11.1.2 from the same pinned tarball, with `--enable-plugins`, under its own cache
  key. The control group's `qemu-system` is unchanged.
- **Plugin** `tests/bench/plugins/indirect.c`, built by the framework against that build's plugin header: indirect
  `call`, indirect `jmp` and `ret` found from the instruction bytes (prefixes included), the target taken from the next
  TB-exec callback, interrupts and exceptions separated by the discontinuity callback; per site: executions, up to 4
  targets with counts and an overflow count; a return-stack model per vCPU (a call pushes its return address, a return
  pops and compares) at depths 8, 16 and 32; user and kernel apart, CR3 read through the register API.
- **Cases** `guest-<b>-census` for chunk 0's benchmarks and `io5-solo32-census` (steps inside the existing 45-minute
  bound), with `tools/bench/run.py`'s like-for-like CPU model (`QCPU`), so glibc picks the same string functions.
- **Report:** shares of indirect `jmp`/`call` executions at one- and two-target sites, the return model's hit rates, and
  each per guest instruction executed (time under TCG means nothing here).

### Chunk 3: the cost of one resident trip to guest code

A program in the bench image (`tools/bench/image/src/coldtrip.c`) runs a fixed work loop and, every K iterations, one
instruction natemu runs in the resident: CPUID at CPL 3 (a system cut: a full trip through the resident). A control runs
the same `i % K` branch with an inline instruction instead. Two footprints: a loop of a few blocks, and one spread over a
few hundred KiB of code. Under the `perf` resident, the guest bucket per iteration against trips per iteration gives a
slope per footprint. The guest bucket runs from just before `resume_exception` through the next entry's `save_native` up
to the entry's charge, so the small
footprint's slope is the gate's fixed cost; the large footprint's slope minus the small one's is the cold-state cost
per trip at that footprint. A CPUID trip touches less resident state than a page fault, a system call or a miss: the
findings say so. D3's answer multiplies that cost by the trips configure makes, against configure's guest bucket, both
from the unperturbed `io5-solo32-profile` run on the same machine. Case `guest-coldtrip-profile`; its judge checks the
program's result and that the trip count matches K.

### Chunk 4: one profile round and its findings

All chunk 0-3 cases once, plus `io5-solo32-profile` for the unperturbed buckets, on one machine (the main runner, the M2
Max); no table mixes machines. A findings page, `el0-sampler-findings.md`, gives the tables per workload and step and
fills the decision table. The guest-code review gets one status line pointing at it.

## 5. The explainer page

Dropped (owner, November 23): the report's JSON and Markdown tables are the output.

## 6. Owner decision: a run-time switch in the normal resident

With the sample bell, the sampler costs nothing until the host rings: the IRQ path gains one INTID compare and
`resume_exception` one conditional store. Shipping it in the normal resident would let any run be profiled without a
rebuild (`--profile FILE`, the signal toggle), Windows later included. It needs the full gate and a same-machine A/B of
the normal build with sampling off against the parent, three alternating rounds, showing no difference beyond noise. The
`perf` buckets stay a measurement build either way: they stamp time at every trip.

## 7. Decision rules

| Measured | Then |
| --- | --- |
| Slot plus its landing words 8% or more of EL0 samples on `build` or a `make` step, and each site's main target taking 60% or more of indirect `jmp`/`call` executions (execution-weighted top-1 share, per address space and by code with targets relative to the site) | design an inline cache per site |
| Returns 5% or more of EL0 samples, and the depth-16 return model hits 90% or more | design return prediction; first find the record of the earlier return-pairing experiment that lost time on 64-bit code (the [task 4 findings](m2-task4-findings.md) point to "the translated-code experiments", the November 15-19 series, whose [page](translated-code-layout.md) does not record it) |
| FEX's own body 60% or more of the hot blocks' samples | a FEX code-quality track goes to the owner, with the guest bucket against the real-x86 reference case's step times beside it |
| Split checks and probes 3% or more | look at which stores keep their own check (bases `--push-run`'s dataflow cannot show aligned) |
| REP string bodies 3% or more | bulk `rep movs` inside a page (the chunk 26 pattern) |
| glibc AVX2 string functions 3% or more | fexc lowering of `vpmovmskb` and the YMM upper halves |
| Flag words 3% or more | flag liveness across extended units |
| Trip cost times trips 10% or more of configure's guest bucket | the resident's return path and footprint |
| Anything under 3% | recorded, left |

## 8. Cost, tests, review

Estimate (low confidence): chunk 0 two days (the sample bell, the ring and its drain, the objects file, the cases), chunk
1 a day, chunk 2 a day (the plugin QEMU build included), chunk 3 half a day, chunk 4 about four hours of machine time plus
the page.

Testing ([testing page](../testing.md)): chunk 0 changes the resident and host under the feature and adds framework cases;
each new check is shown to fail when it should (the validity case with the sample record's block field zeroed; the
report's and plugin's unit tests on wrong input). The host change (the second SPI, the drain, the objects file) is
compiled into every host, so chunk 0 takes the gate once before it merges. Every input is built by the framework: the
bench image with the two new programs, the plugin QEMU from the pinned tarball, the kernel symbols from the
`debian-kernel-deb` resource.

Review (independent, November 23): accept with fixes. Taken in: the sample bell and no stop (every ring stopped the vCPU),
sampling only the sample bell, the timed pending test instead of the `RESUMED` filter, the ring with time instead of one
dump (per-step results), objects from the host instead of the flushed code area, the room left in the study area,
chunk 3's gate cost and control and its denominator on the same machine, the return/indirect split, the FEX-body rule
restored, KASLR recovered, a skid phase, one bench image change, the 4-vCPU case, `QCPU` and per-instruction census
counts. Second round (the same reviewer, on the revision): accept with fixes. Taken in: the bell's resume maps a probe PC to
its block before re-arming (else `BAD_STOP`), halted vCPUs not rung and the waits' race handled, the bell range after
all `MAX_VCPUS` doorbells with its INTID passed to the resident, the ring's ordering, objects for the whole run and the
pool, the offset mapping, clock pairs for markers, guest bytes read through `source_gpa`, the io5 re-run. Added after
that (owner, November 23): the guest's control port (section 3), reviewed on its own (accept with fixes; taken in: the
stopped-window judge by ring time with the pass in flight allowed and samples required on both sides, the corrected
reason for not probing, the port path, `conv=notrunc`, the command-line difference stated).

## 9. Not in this plan

- No translator change; no PMU (HVF gives the guest none); no call stacks (Debian's binaries have no frame pointers).
- No benchmark-page numbers: these are profiles.
- Warm start through the persistent cache (deferred by the owner, November 23).

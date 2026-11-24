# The resident trip's cost: the gate, the return path and the footprint (plan)

Status: design, not built (November 23, 2026), branch `m2-sampler` at d3f2fe4d. It answers the row "trip cost times
trips >= 10% of configure's guest bucket" of the [sampler findings](el0-sampler-findings.md) (section 7: **build**, the
resident's return path and footprint; the gate is the larger part) and the owner's decisions under it: fixes are pooled
(no A/B per fix; one combined A/B and profile round at the end, with the gate), each fix is correct first and
lightweight, and each has its own kill-test. Line numbers are d3f2fe4d's. "Verified" means read in the source at that
commit; everything else is marked unverified, estimate or experiment.

## 1. What is measured, and what is not

`guest-coldtrip-profile` (M2 Max, a7cd8e82, two runs, 12 counted rounds) gives, per CPUID trip, in the `perf`
resident's guest bucket: the **gate's fixed cost 57.7 ns** median (54.2-63.4, stdev 2.9) and the **cold state 33.2 ns**
(21.6-43.0, stdev 7.0) at a few hundred KiB of guest code; the whole trip (every bucket but `halted`) is 378-384 ns small
and 404-412 ns large. The guest bucket runs from the `perf::enter(GUEST)` just before the jump to `resume_exception`
(`execution.rs:2320-2321` for a full entry, `:1055` for a fast miss, `:2493` for `resume`) to the next entry's charge in
`returned` (`execution.rs:2336-2347`), so the 57.7 ns is everything between those two counter reads that a trip adds
over the inline control (`coldtrip_report.py:22-26`, the slope of guest ns per iteration on trips per iteration).

Three things follow and bound the plan:

- The CPUID trip is a literal exit (`svc #LINK`) into a kept system decode: `finish` skips the 800-byte copy into
  `Shared::captured` for it (`main.rs:96-98`, `execution.rs:2407-2411`), `sys_fast` declines it (CPUID is not one of its
  kinds, `execution.rs:841-846`), the linker loads the state record (`:710`), banks, runs the instruction in `find` and
  `enter` writes the record back (`:2313-2318`). So the gate measured is the cheapest gate a trip pays: kinds that resume
  mid-block also copy the frame inside the guest bucket (section 2.2).
- The `perf` hooks inside the guest bucket (the kind match and `perf::enter` before the counter read in `returned`, the
  stores after the read in `perf::enter(GUEST)`: `perf.rs:13-35, 48-51`) exist only in the `perf` resident
  (`perf.rs:98-121`). The default resident's gate is a few ns cheaper than measured (estimate: about 15-25 instructions,
  no barrier). They are constant before and after every change here, so they do not bias a kill-test; cutting them would
  change no production time, so this plan leaves them.
- The cold state is a lower bound (a CPUID trip touches less resident state than a fault, a system call or a miss), and
  nothing measured splits it into causes. Section 3 gives the candidates; the footprint step's kill-test is the only
  measurement of whether the candidate chosen was the right one.

## 2. Where the 58 ns go

### 2.1 The instruction sequence (every trip)

In order, from the guest bucket's start to its end (verified in the source; cycle figures are estimates, unverified:
nothing here measured them):

| Step | Where | What runs | Every trip? |
| --- | --- | --- | --- |
| R1 | `execution.rs:2320-2321`, `perf.rs:32-33` | (perf only) the charge's stores after the counter read; `alignment_check` | yes |
| R2 | `gates.S:248-251` | `enter_allocated`: `msr spsel, #1`, `b resume_exception` | full entries only (`enter`); `resume` and `miss_fast` call `resume_exception` directly (`execution.rs:2494, 2511, 1059`) |
| R3 | `gates.S:171-178` | `cbz x3` (the alignment check's `orr`, `msr sctlr_el1`, `isb` only when x86 #AC checking is on); `msr elr_el1`, `msr spsr_el1` | yes (the `isb` rarely) |
| R4 | `gates.S:187-195` | SP_EL0 from the frame (`ldr`, `msr sp_el0`), SP_EL1 reset (literal `ldr`, `mov sp`), `ldr`+`msr fpcr`, `ldr`+`msr fpsr` | yes: four system-register writes |
| R5 | `gates.S:196-211` | 16 `ldp q` (512 bytes) | yes |
| R6 | `gates.S:212-242` | 31 single `ldr x` (248 bytes; x16, the base, last) | yes |
| R7 | `gates.S:243` | `eret` | yes (hardware) |
| E0 | translated code | the block cut at the system instruction: the literal exit (`exit_literal` words, then `svc #LINK`) and the next block's entry words | the coldtrip trip; other kinds have their own exit words |
| E1 | hardware | the synchronous exception to EL1 (`svc`), vector `+0x400` | yes |
| E2 | `gates.S:112-114` | `b exception_capture` | yes |
| E3 | `gates.S:46-79` | `sub sp`; 15 `stp x` + `str x30` (248 bytes); 16 `stp q` (512 bytes) | yes |
| E4 | `gates.S:80-86` | `mrs fpcr`, `str`, `msr fpcr, xzr` (I5a: the resident never runs under the guest's FPCR); `mrs fpsr`, `str`; `str xzr` | yes: one system-register write |
| E5 | `gates.S:89-94` | `mrs sctlr_el1`, `tbz` (the `bic`, `msr`, `isb` only when SCTLR_EL1.A is set) | yes (the `isb` rarely) |
| E6 | `gates.S:99-106` | `mrs sp_el0`, `str`; `mrs spsr_el1`, `and`, `str` (NZCV); `bl finish` | yes |
| E7 | `main.rs:91-101` | `finish`: `fp::cancel`'s marker load (`fp.rs:72-75, 234-239`), `frame_only` (`mrs esr_el1`, `execution.rs:2407-2411`), the 800-byte copy into `Shared::captured` unless `svc #LINK`, `svc #MISS` or an FP trap, the mode load, `bl returned` | yes; the copy: every kind but those three |
| E8 | `execution.rs:2333-2347` | `returned`: `mrs` ESR, FAR, ELR, SPSR (the second read of ESR and SPSR); (perf only) the kind match, the count, the counter read | yes |

Counted: about 190 instructions on the CPUID trip (estimate from the source, not from the binary), 1.6 KiB of frame
traffic (800 bytes stored at the gate, 800 loaded at the return, from `Shared::initial` after a full entry or from the
gate's frame after a fast miss), 7 system-register writes (`msr fpcr` twice, `fpsr`, `elr_el1`, `spsr_el1`, `sp_el0`,
`spsel`), about 10 system-register reads, no barrier in the common case, one exception entry and one `eret`.

What the source settles about the usual suspects:

- **Barriers.** None on the CPUID trip: the two `isb`s of the gate and return (E5, R3) run only under x86 alignment
  checking (CR0.AM, RFLAGS.AC, CPL 3: `execution.rs:2667-2669`), and the counter read has none (`perf.rs:15-16`).
- **`tables::select`.** Not in the guest bucket (it runs before `perf::enter(GUEST)`, `execution.rs:2319`), and when the
  class is unchanged it is one `mrs ttbr0_el1` and a compare (`tables.rs:77-85`). Its three `isb`s (`tables.rs:90-94`)
  run only when the privilege class or the compat root changes (system calls, deliveries, IRET, interrupts): about 31 ns
  each in chunk 17's sub-buckets (perf-plan, chunk 17), charged to `enter`. Not this plan's (section 6).
- **FPCR.** On a host with FEAT_AFP the guest's FPCR always has AH and NEP set (`fpcr`, abi `execution.rs:2639-2646`), so
  both FPCR writes change the register at every trip on the M2 and the M4; a skip-if-equal helps only hosts without AFP
  (the M1: there the default MXCSR gives FPCR 0). FPSR is written once (R4); at a full entry the frame's FPSR is 0
  (`sse.rs:30`), as is the resident's own FPSR after a trip with no FP arithmetic (estimate: nearly all trips).
- **The floor.** E1 and R7 (exception entry, `eret`, the refetch behind each) and the save and restore the design needs
  (the resident is compiled code and may use any register: the gate cannot know what it will clobber) are not removable
  by this plan. How large that floor is was never measured on these chips (unverified; the optional experiment X1 in
  section 5 measures it). An estimate from the source: about half of the 58 ns is the hardware's (E1, R7 and the
  pipeline refill behind each), the other half the 190 instructions and their system-register accesses.

### 2.2 What only some kinds pay

| Cost | Kinds | Where charged | Source |
| --- | --- | --- | --- |
| The 800-byte copy into `Shared::captured` (25 rounds of two `ldp` and two `stp`, `record.rs:9-31`) | every kind but `svc #LINK`, `svc #MISS` and FP traps: aborts, helpers, divisions, interrupts, stops, steps | guest (before the kind's charge) | `main.rs:96-98` |
| The state record loaded (about 1.4 KiB copied to the stack) | linker (`execution.rs:710`), `missed` (`:2416`), division (`:2461`), helpers (`helpers.rs:13, 28`), aborts | the kind's bucket | `record.rs:33-38` |
| The record written back (`enter_copy`) | every full entry not from `sys_fast` | `enter_copy` | `execution.rs:2312-2318` |
| `msr spsel, #1` | full entries | guest | `gates.S:250` |
| The shadow-space switch, three `isb` | class changes | `enter` | `tables.rs:86-94` |

### 2.3 What can be cut exactly (and what cannot)

| Cut | Exact because | Estimated gain (unverified) |
| --- | --- | --- |
| R6: the 31 GPR loads as 15 `ldp` and two `ldr` (x16 still last) | same values into the same registers from the same offsets; loads only, after the SP reset, so the layout contract (`gates.S:162-166`) holds | 14 fewer load instructions: 1-3 ns |
| E7, E8: ESR, ELR and SPSR read once in the gate and passed to `finish`, `frame_only` and `returned` in registers (FAR too) | the registers do not change between the gate and `returned` (interrupts are masked, nothing in between takes an exception); the second `mrs` of each reads the same value | 4-5 fewer `mrs`: 1-3 ns |
| R2: `msr spsel, #1` only when SPSel is 0 | after the first entry every resident run is on SP_EL1 (an exception to EL1 selects it), so the write changes nothing; only the boot's first entry (`start`, on SP_EL0 from `_start`, `gates.S:36-40`) needs it | 0-5 ns (the cost of a PSTATE write here is unknown) |
| R4: `msr fpsr` only when the frame's FPSR differs from the register; E4: `msr fpcr, xzr` only when FPCR is not already 0 | writing a value a register already holds is architecturally a no-op | FPSR: 0-5 ns on most trips; FPCR: M1-class hosts only |
| The 800-byte copy, for division and helpers: resume from the gate's frame, not `Shared::captured` | the frame is intact until the `eret`: it lies above SP, nothing nested enters (a fault in the resident halts, `execution.rs:2349-2351`), and `resume_exception` already resumes from it for fast misses (`gates.S:162-166`, `execution.rs:1059`); division and the helpers read their inputs before they write their results | 10-15 ns per such trip (25 rounds of 32 bytes, about 100 instructions), in the guest bucket |
| The state record's copy, for division and helpers: read the record in place | they only read the record on their resuming path (`resume` takes `&State`, `execution.rs:2484`); their halts already copy (`halt_native`, `:2439-2444`) | 10-15 ns per such trip (half of chunk 22's 24 ns out and back) |
| Saving v0-v15 only for mid-block kinds | at a block boundary v0-v15 are FEX's temporaries (XMM is in v16-v31, `sse.rs:25-31`; the entry frame's v0-v15 are zero, `execution.rs:2224-2310`) | **rejected**: the gate would first branch on ESR's class, a branch that alternates with the trip kind (an estimated mispredict costs about what the eight `stp q` save), and the ERET audit would need two save shapes |
| SP_EL0's save and restore | generated code never uses SP (`f.sp = STACK_TOP`, `execution.rs:2309`; host admission) | **rejected**: the restore at every full entry is the containment reset of SP_EL0; two instructions are not worth weakening it |
| FPCR left as the guest's in the resident (lazy FPCR) | it is not: the resident's FP and x87 helpers assume FPCR 0 (I5a), and accurate mode's trap enables would trap in the resident (`execution.rs:2447-2456`) | **rejected** for this plan: it would need a static proof that no FP arithmetic runs before a lazy write; X1 says whether the two FPCR writes are worth that |

So the exact software cuts reach roughly 4-15 ns of the 58 on the CPUID trip, and 25-40 ns on division and helper
trips. The rest is the floor and the save and restore the design keeps.

## 3. Where the 33 ns of cold state go

The cold state is the extra guest-code time per trip when the guest's code spreads over 256 KiB of x86 (`coldtrip.c:10-14`,
512 functions run in 64 chunks): the large footprint misses L1I at every iteration even without trips, so the figure is
what a trip adds on top (`coldtrip_report.py:30-33`). K = 1 and 2 dominate the fit, so the resident's own code runs every
iteration or every other one and stays in L1I; what a trip disturbs is what the guest would otherwise keep. Candidates,
none measured:

- **Instruction TLB entries.** The resident's text is one 2 MiB stage-1 block (`linker.ld`: `.text` at PRIVATE + 2 MiB;
  `memory.rs:321-350`: 2 MiB blocks above the window's first 2 MiB), but under Hypervisor.framework the effective entry
  is at most the stage-2 granule (16 KiB on macOS: unverified). An experiment on a resident built in the main checkout
  on November 22 (not d3f2fe4d's binary; layout only): the functions the CPUID trip runs spread over 0x203648-0x226ad0 of
  `.text` plus `natemu_abi::cpu::cpuid` at 0x237238 (`linker` 0x20535c, `system` 0x2058fc, `returned` 0x207d1c,
  `cache::lookup` 0x20b208, `block_at` 0x2115fc, `tables::select` 0x21a7c4, `copy_record` 0x21d270,
  `exception_capture` 0x220800, `vectors` 0x221000, `finish` 0x2243cc, `sse::bank` 0x225bb4): about 12 granules of 16 KiB
  (or about 40 pages of 4 KiB) for about 25 KiB of code. The guest's translated code in the large footprint needs many
  entries of its own; each entry the trip takes is one the guest walks again (a nested walk under stage 2).
- **Branch predictor and return stack state**: the trip runs a few hundred branches. Not something a layout fixes;
  a shorter path helps a little.
- **Data lines and data TLB entries**: the trip touches the gate frame (13 lines), `Shared::initial` (13), the state
  record (about 22, copied to the stack: 22 more, and written back), the frame page, `Control`, the block index, the
  `perf` record. In coldtrip the guest's data is small, so this part hardly shows there; in configure (large guest data)
  it likely matters more (unverified). The data sit in different pages: `Shared` at PRIVATE + 0x20000, the state at
  + 0x22000 (abi `lib.rs:14`, abi `execution.rs:86`), the exception stack's top at + 0x70000 (`lib.rs:20`).

What can be cut lightly: put the trip path's code in one contiguous, 16 KiB-aligned run (the instruction TLB and L1I
candidate). Moving the data together is an abi layout change touching the host and the static checks: not lightweight,
left for later (section 6). Copying less (section 2.3, and the linker in place, section 4 step 4) also shrinks the data
footprint.

## 4. Which trips, and whether to cut their number instead

Configure's trips (findings section 6, estimates from bucket time over each kind's launch-wide mean cost): 65.2 M, of
which **dispatcher misses 29.0 M, helpers 14.1 M, links 11.8 M, aborts 10.2 M**. Resident time per entry, launch-wide (the
kind's buckets, the gate excluded): link 428 ns, miss 127, helper 132, abort 519. The findings give make's total only
(187.9 M; i386-make 202.7 M). The [architecture review](architecture-review-20261113.md) (November 13, an older commit,
make at 4 vCPUs: not this page's machine or commit, for the shape only) had make's 327 M as misses 132 M, links 66 M,
helpers 65 M, aborts 42 M, system 13 M.

With the gate (58) and at least 33 ns of cold state, a whole trip costs about 220 ns (miss, helper) to 520-610 ns (link,
abort). Removing one trip saves 4 to 40 times what this plan can take off one. But every count reduction at hand is
another, larger project, mostly root cause A of the architecture review (currency per address space: configure's misses,
aborts and remade links follow its CR3 loads), and the count cuts already decided are pooled with this fix (indirect
step 1 and return prediction take computed exits off the dispatcher; bulk REP strings take string iterations off
the resident). One cheap step on counts belongs here: the helper bucket merges `svc #X87` and `svc #STRING`
(`execution.rs:2345`); `svc #STRING` is PCMPxSTRx (`helpers.rs:24-45`), not REP strings, and nobody knows which of the two
makes configure's 14.1 M. Splitting the count (a `perf`-only counter) in the pooled profile round tells whether inlining
PCMPxSTRx in fexc is worth a plan. So: the per-trip cut is built (every remaining trip pays it, now and after the pooled
fixes), the count is left to its own plans.

## 5. What to build, smallest first

Each step is one fix with its own tests and kill-test; they land in this order, and the pooled A/B and profile round
at the end measure them together. All kill-tests run on the M2 Max (the main runner), one case run before (at the fix's
parent) and one after; they are profiles, never benchmark-page numbers.

### Step 1, the smallest: the lean gate (every trip)

1. `resume_exception` restores x0-x15 and x18-x29 with `ldp`, x17 and x30 with `ldr`, x16 last (`gates.S:212-242`);
   the profile build's resume-time store stays where it is (`gates.S:179-186`, before `mov sp`).
2. Both gates read ESR, FAR, ELR and SPSR once after the save (x2-x5) and pass them to `finish`, which passes ESR to
   `frame_only` and all four to `returned` (`main.rs:91-101`, `execution.rs:2329-2335, 2407-2411`); `exception_capture`'s
   NZCV store uses the SPSR read. `aborted` keeps its own reads (`execution.rs:3845`; not on every trip).
3. `enter_allocated` writes SPSel only when it reads 0 (`gates.S:248-251`).
4. `resume_exception` skips `msr fpsr` when the register already holds the frame's value, and the save skips
   `msr fpcr, xzr` when FPCR is already 0 (`gates.S:80-85, 194-195`). The FPCR restore stays unconditional (on AFP hosts
   it always changes the register: section 2.1).

Kill-test: `guest-coldtrip-profile`'s `gate_ns` median before and after. **Kill if the median is not at least 3 ns
lower** (about twice the median's standard error over six rounds at the measured stdev of 2.9 ns). Expected 4-12 ns,
less if the system-register writes are cheap on this chip (unverified).

### Step 2: division and helpers without copies

1. `finish` skips the copy for `svc #DIV_U`, `#DIV_S`, `#X87` and `#STRING` too (`frame_only`, `execution.rs:2407-2411`).
2. `division`, `helpers::x87` and `helpers::string` take the gate's frame as `*mut NativeFrame`, read their inputs
   through it, write their results into it and pass it to `resume` (`execution.rs:2458-2474`, `helpers.rs:11-45`); no
   `Shared::captured` write on these paths.
3. They read the state record in place (`&*record()`), as `sys_fast` and `fp::stepped` do (`execution.rs:835`,
   `fp.rs:213`); a halt still goes through `halt_native`, which copies. Their frames lose the state copy (the stack bound
   falls).

Kill-test: `k2-cost` (the production resident, wall-clock slope per iteration, `run_k2.py:199-223`) with `--cost-rows
div,cpuid`, before and after: **kill if `div` is not at least 10 ns per iteration faster**; `cpuid` must not be slower
beyond its spread (it is step 1's and is the control here). Expected 20-30 ns on `div`.

### Step 3: the trip path's code in one place (the footprint)

1. A `.text.trip` input section placed first in `.text`, 16 KiB-aligned, in `linker.ld`; `gates.S`'s `.section .text`
   (`gates.S:45`) becomes `.text.trip` (the vectors keep `.balign 2048`).
2. `#[link_section = ".text.trip"]` on the functions a common trip runs, in path order: `finish`, `returned`,
   `sys_fast`, `linker`, `miss_fast`, `missed`, `bank`, `sse::bank`, `sse::fill`, `find_from` and `enter` (if out of line),
   `cache::{block_at, current_at, lookup, select_l1, fill_l1}`, `tables::select`, `copy_record`, `events::{none, take}`,
   `lapic::poll`, `post_pending`, `io_checkpoint`, `take_invalidations`, `resume`, `division`, `helpers::{x87, string}`,
   `aborted`, `access_aborted`. From the November 22 binary's sizes (experiment) that is about 25-32 KiB: two granules.
   Whether `#[link_section]` covers every instance of the generic `cache::lookup` is unverified; if not, those instances
   are placed by name in `linker.ld`.
3. The static `stack` case also checks that every listed function lies inside `.text.trip` and that `.text.trip` is at
   most 32 KiB (`check_resident_stack.py` reads the disassembly and symbols already).

Kill-test: the same `guest-coldtrip-profile` run as step 1's after-run, against a run after step 3: **kill if `cold_ns`'s
median is not lower by at least 7 ns** (the rounds' stdev). Expected 0-20 ns (the instruction-TLB candidate is a guess:
section 3). The run's `trip_small_ns` must not rise beyond its spread.

### Step 4, only if steps 1-3 leave the gate and footprint as the larger part: the linker in place

The literal exit's service on the record itself, as `sys_fast` does (no `load` at `execution.rs:710`, no write-back at
`:2312-2318`): chunk 22 estimated the record's copy out and back at about 24 ns per trip, and it is 2.8 KiB of data the
trip stops touching. Not lightweight: `find` can ask the host (`ask`'s write-out and reload around a host exit) and every
reader of `record()` during the path must be shown not to need the last full entry's values. Its own design note first;
kill-test `trip_small_ns`.

### Optional experiment X1: the gate's floor

A measurement-only resident feature (not kept): an `svc` immediate the gate turns straight around (`save_native`, then
`resume_exception` from the gate frame at ELR, no Rust), and a coldtrip variant that issues it. The slope is the floor
the hardware and the full save and restore cost; the gate's 58 ns less it is everything above any gate design. One run,
numbers called an experiment. Worth its 2 hours only if step 1's kill-test is ambiguous or someone proposes an assembly
fast gate.

## 6. Considered and left

- An assembly-only dispatcher-miss path that saves nothing (only x0-x2 are live at the miss, `gates.S:253-257`): the
  largest possible cut for 29 M misses, but `miss_fast`'s every check (debug, boundary, pending work, I/O window, LAPIC,
  events, uncached shadows: `execution.rs:973-1066`) would move into assembly. Not lightweight.
- `tables::select`'s third `isb` before the `eret`: an `eret` is a context synchronization event only when SCTLR_EL1.EOS
  is set or FEAT_ExS is absent (unverified here; the resident records SCTLR at its start, `main.rs:64-67`). Worth about
  10 ns per class change; it needs that premise checked at start and a system-call kill-test (`k2-cost`'s `syscall` row).
- Hot data in adjacent pages (`Shared`, the state record, the exception stack's top): an abi layout change. Its
  candidate effect is on configure's large guest data, which coldtrip cannot show; after step 3's result.
- An inline PCMPxSTRx in fexc: after the helper split (section 4) says how many of the 14.1 M it is.

## 7. Correctness

Exact means: at every fault and every resume, the guest's registers, flags and FP state, and the resident's own
invariants, are what they are today. Per item:

- **Frame contents.** Step 1.1 loads the same offsets into the same registers (`NativeFrame` layout, abi `lib.rs:52-60`;
  the checker's expected sequence, `check_resident_stack.py:240-244`, is rewritten to the pairs, with x16 last). Step 2
  writes the results (division's x0-x2, `string`'s x0) into the gate frame instead of `captured`, after reading every
  input; nothing reads the frame after `resume_exception` starts. Rust aliasing: the services take the frame as a raw
  pointer and form no `&NativeFrame` that outlives a write through it (today `f: &NativeFrame` and the write through
  `captured` do not alias; after step 2 they would).
- **NZCV.** From SPSR, read once (step 1.2) and stored as today (`gates.S:101-103, 154-156`); every resume builds PSTATE
  as today (`BLOCK_SPSR | nzcv`, `execution.rs:2321, 1062`; the frame's SPSR for `resume`).
- **FPCR and FPSR.** The resident still runs under FPCR 0 (I5a): the save writes 0 unless FPCR is 0 already. The resume
  leaves FPSR as the frame says, by skipping only a write of the value it holds. `fp::cancel` still edits the frame's
  FPCR and FPSR before anything reads them (`main.rs:95`, `fp.rs:250-251`); with step 2, FP traps keep their own copy
  (`fp.rs:173-178`), unchanged.
- **SP_EL0.** Saved and restored as today. SPSel (step 1.3): written whenever it reads 0, so the boot's first entry
  switches as today; the stack checker's stack switch for `enter_allocated` (`check_resident_stack.py:52-54`) stays, now
  conditional.
- **Alignment checking.** Unchanged: the gate clears SCTLR_EL1.A before resident code (`gates.S:87-94`), the resume sets
  it from x3 (`gates.S:169-176`); every resume still passes `alignment_check` (`execution.rs:2494, 2511, 1063`).
- **Guest space selection.** Unchanged: `select_guest_space` before every resume (`execution.rs:2492, 2509, 1054, 2319`).
- **Stop arming.** `resume` still arms a wanted stop in the block holding the PC (`execution.rs:2485-2490`); step 2's
  in-place record gives it the same `stop_wanted`.
- **`shadow_entry`.** Written only by `enter` and `miss_fast` (`execution.rs:2131-2135, 2306-2308, 1049`); no step changes either.
- **Interrupts.** The resident runs with IRQs masked (an exception entry sets PSTATE.I); the syndrome registers cannot
  change between the gate and `returned`. The IRQ gate passes the same registers; `returned` reads ESR only for
  kind 1 (`execution.rs:2337-2347`, `finish`'s `fault == 1` test).
- **The stack bound** (static `stack` and `stack-profile`, contract 62): step 1 adds register arguments only; step 2
  removes a state copy from three frames; step 3 moves code, not frames. The ERET audit's expected save and restore
  sequences (`check_resident_stack.py:240-244, 272-279`) are updated with the change, in the same commit.

Families that cover each path (the affected tier runs them; the gate runs everything before the pooled merge):

| Path | Families and cases |
| --- | --- |
| The gate and the return (every kind) | static `stack`, `stack-profile`; programs (`fix`, `seed`, `i9`, `i10`, `rex`, `i7`, `xsave`: registers, XMM, flags across blocks); system `k1-*`, `k2-*`; boot `boot-smoke`, `boot-i386`; correctness `x86st`, `torture`, `i386` |
| Literal exits, misses, `sys_fast` | system `k1-*`, `k2-sysfast`, `k2-swapgs`; smp `events`, `delivery`, `contain`, `privilege` |
| FPCR and FPSR | faults `i5c`, `i5a-de-*`, `i6-faults`, `i8-faults`; fp `i8`, `i8b`, `approx-special-accurate`, `approx-special-fast`; unit `rust-afp` |
| Alignment checking | smp `vector-ac`; the K1 program `miss-ac` |
| Division | `k2-cost` (`div`); programs corpora with 128/64 division (which ones divide with RDX != 0 is unverified: the implementer confirms with mutation T5) |
| Helpers | programs `i6b`, faults `i6b-cases` (x87 transcendental and PCMPxSTRx, `helpers.rs:1-6`) |
| Interrupts | system `x3-ipi`, `x3-timers`, `x3-stress`; smp `events` |
| The footprint | static `stack` (the new placement check) |

## 8. Tests with mutations

The mutations go into `tests/system/run_fastmiss_mutations.py`'s table (its sources list gains `gates.S` and `main.rs`;
it already runs other runners' programs, `slice3:` and `m2:`), or into the static checker's own unit tests
(`tests/static/test_check_resident_stack.py`) where the static case is the one that must go red. Each must go red; one
that stays green is a defect of the tests.

| # | Step | Mutation | Must go red |
| --- | --- | --- | --- |
| T1 | 1.1 | an `ldp` restoring x14 and x15 from x12's offset | static `stack` (checker unit test on the disassembly), and K1 `contexts` |
| T2 | 1.1 | x16 restored before x17 | static `stack` |
| T3 | 1.2 | `returned` given the ELR read before the save's last store, from x2 instead of x4 (ESR as ELR) | any K1 program (every exit halts `bad_link`) |
| T4 | 1.4 | the FPSR skip inverted (write only when equal) | faults `i5c` (MXCSR flags from FPSR) |
| T5 | 2 | division writes its quotient to `Shared::captured` and resumes from the gate frame | `k2-cost` `div` (its R8 check sees a wrong loop count) and a programs case with a 128/64 division |
| T6 | 2 | `finish` skips the copy for `svc #X87` while `helpers::x87` still resumes from `captured` | programs `i6b` |
| T7 | 1.3 | the SPSel test reads the wrong bit (never writes) | boot `boot-smoke` (the first entry resumes on SP_EL0) |
| T8 | 3 | `#[link_section]` removed from `returned` | static `stack` (the placement check) |
| T9 | 1.4 | the FPCR save skip taken always (the resident runs under the guest's FPCR) | fp `approx-special-accurate` on a host without AFP; on AFP hosts faults `i8-faults` (x87 helpers under AH/NEP) (unverified which turns red first: the implementer records it) |

## 9. Estimate (agent hours)

| Step | Work | Hours |
| --- | --- | --- |
| 1 | the four gate cuts, the checker's sequences, T1-T4, T7, T9; affected tier; kill-test runs | 4 |
| 2 | the three services in place, `frame_only`, T5, T6; the aliasing audit; kill-test runs | 3 |
| 3 | `.text.trip`, the attributes, the placement check, T8; kill-test run | 3 |
| Reviews | an independent review of each step's diff (AGENTS.md: authors and reviewers separate) | 2 |
| X1 (optional) | the floor experiment | 2 |
| 4 (conditional) | the linker in place: a design note first | 6-8 |

Steps 1-3 with reviews: about 12 hours, plus machine time for three coldtrip runs and two `k2-cost` runs.

## 10. Expected gain, and why

- Step 1: 4-12 ns per trip, every trip. Configure: 65.2 M trips, 0.26-0.78 s of its 30.35 vCPU-s guest bucket
  (0.9-2.6%); make: 187.9 M, 0.75-2.3 s of 299.7 (0.3-0.8%). The range is the unknown cost of system-register writes.
- Step 2: 20-30 ns per helper or division trip (10-15 in the guest bucket, the copy; 10-15 in the helper bucket, the
  record). Configure's 14.1 M helpers: 0.28-0.42 s.
- Step 3: 0-20 ns of cold state per trip (0-60% of 33): configure 0-1.3 s (0-4.3% of its guest bucket).
- Together, configure: about 0.5-2.5 s, 2-8% of its guest bucket, against the 19.5% (16.3-22.9) gate plus cold state
  today; make: about 1-4 vCPU-s. The gap is the floor (section 2.1): this plan takes the software above it and the
  footprint it can move cheaply, and leaves the count to the plans that own it (section 4).

# The resident trip's cost: the gate, the return path and the footprint (plan)

Status: design, not built (November 23, 2026), branch `m2-sampler` at d3f2fe4d; revised after an independent review (SPSR is not passed from the gate, kill-tests by three alternating runs, the FPCR skip dropped, helper trips owned here). It answers the row "trip cost times
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
  (`sse.rs:30`), but the live register still holds the guest's sticky flags from before the trip (the gate saves FPSR
  and clears nothing), so after FP code the write changes it.
- **SPSR is not stable across the gate.** `finish` runs `fp_cancel` (`main.rs:95`) before `returned` reads SPSR_EL1
  (`execution.rs:2333`), and `fp::abandon` rewrites SPSR_EL1 without SS and I (`fp.rs:253-255`). ESR, FAR and ELR are
  stable.
- **The floor.** E1 and R7 (exception entry, `eret`, the refetch behind each) and the save and restore the design needs
  (the resident is compiled code and may use any register: the gate cannot know what it will clobber) are not removable
  by this plan. How large that floor is was never measured on these chips (unverified; experiment X1 in
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
| R6: the 31 GPR loads as 15 `ldp` and two `ldr` (x16 still last) | same values into the same registers from the same offsets; loads only, after the SP reset, so the layout contract (`gates.S:162-166`) holds | 14 fewer load instructions: about 1 ns |
| E7, E8: ESR, FAR and ELR read once in the gate and passed to `finish`, `frame_only` and `returned` in registers. **Not SPSR**: `returned` keeps its own read | ESR, FAR and ELR do not change between the gate and `returned`: interrupts are masked and a nested EL1 fault halts (`execution.rs:2348-2350`). SPSR does: `finish` calls `fp_cancel` (`main.rs:95`) before `returned` reads SPSR (`execution.rs:2333`), and `fp::abandon` rewrites SPSR_EL1 without SS and I (`fp.rs:253-255`); a gate-read SPSR would resume an abandoned FP re-run single-stepping with IRQs masked (hosts without AFP, with trap enables). The gate's NZCV store is unaffected (abandon changes neither N, Z, C nor V) | 3 fewer `mrs`: about 1 ns |
| R2: `msr spsel, #1` only when SPSel is 0 | the only SPSel writes are `gates.S:40` (boot, to 0) and `:250`; the vCPU starts at EL1h (`host/src/vm.rs:490`, CPSR 0x3c5) and every exception to EL1 selects SP_EL1, so after the boot's first entry the write changes nothing | unknown: the test adds an `mrs` and a branch, so it is kept only if X1 or its own kill-test shows the write costs more |
| R4: `msr fpsr` only when the frame's FPSR differs from the register | writing a value the register already holds is a no-op. The win is unsure: `enter` zeroes the frame's FPSR (`sse.rs:30`) while the live register keeps the guest's sticky flags from before the trip (the resident clears nothing), so at a full entry after FP code the two often differ | unknown, like R2: kept only if measured |
| E4: `msr fpcr, xzr` only when FPCR is not 0 | exact, but **dropped**: on AFP hosts `fpcr` always sets AH and NEP (abi `execution.rs:2645`), so on the M2 and the M4 it is a pure extra branch | none on the M2 and M4 |
| The 800-byte copy, for division and helpers: resume from the gate's frame, not `Shared::captured` | the frame is intact until the `eret`: it lies above SP, nothing nested enters (a fault in the resident halts, `execution.rs:2349-2351`), and `resume_exception` already resumes from it for fast misses (`gates.S:162-166`, `execution.rs:1059`); division and the helpers read their inputs before they write their results | 10-15 ns per such trip (25 rounds of 32 bytes, about 100 instructions), in the guest bucket |
| The state record's copy, for division and helpers: read the record in place | they only read the record on their resuming path (`resume` takes `&State`, `execution.rs:2484`); their halts already copy (`halt_native`, `:2439-2444`) | 10-15 ns per such trip (half of chunk 22's 24 ns out and back) |
| Saving v0-v15 only for mid-block kinds | at a block boundary v0-v15 are FEX's temporaries (XMM is in v16-v31, `sse.rs:25-31`; the entry frame's v0-v15 are zero, `execution.rs:2224-2310`) | **rejected**: the gate would first branch on ESR's class, a branch that alternates with the trip kind (an estimated mispredict costs about what the eight `stp q` save), and the ERET audit would need two save shapes |
| SP_EL0's save and restore | generated code never uses SP (`f.sp = STACK_TOP`, `execution.rs:2309`; host admission) | **rejected**: the restore at every full entry is the containment reset of SP_EL0; two instructions are not worth weakening it |
| FPCR left as the guest's in the resident (lazy FPCR) | it is not: the resident's FP and x87 helpers assume FPCR 0 (I5a), and accurate mode's trap enables would trap in the resident (`execution.rs:2447-2456`) | **rejected** for this plan: it would need a static proof that no FP arithmetic runs before a lazy write; X1 says whether the two FPCR writes are worth that |

So the exact software cuts that are sure to pay reach about 1-4 ns of the 58 on the CPUID trip (R6 and the three
`mrs`), more only if R2 and R4 prove to cost, and 20-30 ns on division and helper trips. The likeliest large software
item is the two FPCR writes, which change the register at every trip on AFP hosts and cannot be cut exactly without the
lazy-FPCR proof; experiment X1 measures them first (section 5). The rest is the floor and the save and restore the
design keeps.

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
left for later (section 6). Copying less (section 2.3, and the linker in place, section 5 step 4) also shrinks the data
footprint.

## 4. Which trips, and whether to cut their number instead

Configure's trips (findings section 6, estimates from bucket time over each kind's launch-wide mean cost): 65.2 M, of
which **dispatcher misses 29.0 M, helpers 14.1 M, links 11.8 M, aborts 10.2 M**. Resident time per entry, launch-wide (the
kind's buckets, the gate excluded): link 428 ns, miss 127, helper 132, abort 519. The findings give make's total only
(187.9 M; i386-make 202.7 M). The [architecture review](architecture-review-20261113.md) (November 13, an older commit,
make at 4 vCPUs: not this page's machine or commit, for the shape only) had make's 327 M as misses 132 M, links 66 M,
helpers 65 M, aborts 42 M, system 13 M.

With the gate (58) and at least 33 ns of cold state, a whole trip costs about 220 ns (miss, helper) to 520-610 ns (link,
abort). Removing one trip saves 50 to 500 times what the sure gate cuts take off one (section 2.3).

- **Misses, links, aborts.** Their count reductions are other, larger projects, mostly root cause A of the architecture
  review (currency per address space: configure's misses, aborts and remade links follow its CR3 loads), or are already
  pooled with this fix (indirect step 1 and return prediction take computed exits off the dispatcher; bulk REP strings
  take string iterations off the resident). Not this plan's.
- **Helpers: this plan owns them, and they are its largest item.** Configure's 14.1 M helper trips cost about
  14.1 M x (132 + 58 + 33) ns = **3.1 s**, more than everything steps 1-3 can save (section 10). No other plan owns
  them: `svc #X87` is the x87 transcendental, remainder, scale and extract instructions, `svc #STRING` the PCMPxSTRx
  intermediate result (`helpers.rs:1-6, 24-45`), not REP strings, so the bulk-REP fix does not touch them. The helper
  bucket merges the two (`execution.rs:2345`), so which one makes the 14.1 M is unknown. Step 0 splits the count; step 5
  removes the dominant kind's trips.

## 5. What to build, in order

Each step is one fix with its own tests and kill-test; the pooled A/B and profile round at the end measure them together.

**Kill-test protocol** (every step). On the M2 Max (the main runner), at least three runs of the step's case before (at
the step's parent) and three after, alternating (before, after, before, ...). The per-round values of the three runs of
each side are pooled (coldtrip: 6 counted rounds a run, 18 a side) and the means compared. A step is **killed** unless
its figure moves by more than three standard errors of the difference and by at least the step's minimum. At the
measured stdevs that is about 3 ns for `gate_ns` (2.9 x sqrt(2/18) = 1.0 ns, times 3) and about 7 ns for `cold_ns`
(7.0 x sqrt(2/18) = 2.3 ns, times 3). These are profiles, never benchmark-page numbers; the runs and their commits go in
the step's commit message.

### Step 0, first and nearly free: split the helper count

A `perf`-only counter per helper kind (`svc #X87` and `svc #STRING`, in `returned`'s `svc` arms, `execution.rs:2391-2392`,
under `cfg(feature = "perf")`, reported by the host like `system_from`'s extra slot, perf chunk 22), so
`io5-solo32-profile` gives each kind's exact count per launch, and per step by the findings' method. No kill-test (it
changes no default build); its test is T11. It decides step 5.

### Experiment X1, before step 1: the gate's floor and its system-register writes

A measurement-only resident feature (not kept): an `svc` immediate the gate turns straight around (`save_native`, then
`resume_exception` from the gate frame at ELR, no Rust), and a coldtrip variant that issues it. Three variants of the
turn-around: (a) as the gate and return are today; (b) without the two FPCR writes, which change the register at every
trip on AFP hosts and are the likeliest large software item; (c) (a) plus an `msr spsel, #1` and an `msr fpsr` of the
values the registers hold. (a)'s slope is the floor of the hardware and the full save and restore; 58 ns less (a) is
everything above any gate design; (a) less (b) is the FPCR writes; (c) less (a) the two writes steps 1.3 and 1.4 would
skip. One case run per variant, alternating, the numbers called an experiment. If (a) less (b) is 5 ns or more, a lazy
FPCR (the resident writes FPCR 0 only before its own FP arithmetic, with a static check that no FP arithmetic runs
before that write) gets its own design note.

### Step 1: the lean gate (every trip)

1. `resume_exception` restores x0-x15 and x18-x29 with `ldp`, x17 and x30 with `ldr`, x16 last (`gates.S:212-242`);
   the profile build's resume-time store stays where it is (`gates.S:179-186`, before `mov sp`).
2. Both gates read ESR, FAR and ELR once after the save (x2-x4) and pass them to `finish`, which passes ESR to
   `frame_only` and all three to `returned` (`main.rs:91-101`, `execution.rs:2329-2335, 2407-2411`). **`returned` keeps
   its own SPSR read** after `fp_cancel` (section 2.1); the gate's NZCV store keeps the gate's SPSR read (abandon leaves
   NZCV alone). `aborted` keeps its own reads (`execution.rs:3845`; not on every trip).
3. Only if X1 (c) shows the SPSel write costs at least 3 ns: `enter_allocated` writes SPSel only when it reads 0
   (`gates.S:248-251`), with its own kill-test by the protocol above.
4. Only if X1 (c) shows the FPSR write costs at least 3 ns: `resume_exception` skips `msr fpsr` when the register holds
   the frame's value (`eor` into x10 and `cbz`; x10 is reloaded from the frame later), with its own kill-test.

Kill-test for 1.1 and 1.2 together: `guest-coldtrip-profile`'s `gate_ns` by the protocol, minimum 3 ns. Expected about
1-2 ns (R6 about 1 ns, three `mrs` about 1 ns), which the protocol may well not resolve: then 1.1 and 1.2 are killed and
do not land; 1.3 and 1.4 qualify, or not, on their own.

### Step 2: division and helpers without copies

1. `finish` skips the copy for `svc #DIV_U`, `#DIV_S`, `#X87` and `#STRING` too (`frame_only`, `execution.rs:2407-2411`).
2. `returned` passes **`frame_ptr` itself** (its argument, `execution.rs:2329`) to `division`, `helpers::x87` and
   `helpers::string`, never a pointer cast from `frame` (`returned` holds `frame = &*frame_ptr`, `:2331`, which is not used
   after the call). They read their inputs through it, write their results into it and pass it to `resume`
   (`execution.rs:2458-2474`, `helpers.rs:11-45`); no `Shared::captured` write on these paths.
3. They read the state record in place (`&*record()`), as `sys_fast` and `fp::stepped` do (`execution.rs:835`,
   `fp.rs:213`); a halt still goes through `halt_native`, which copies. Their frames lose the state copy (the stack bound
   falls).

Kill-test: `k2-cost` (the production resident, wall-clock slope per iteration, `run_k2.py:199-223`) with `--cost-rows
div,cpuid`, by the protocol (three alternating runs a side, each rep's slope pooled), minimum 10 ns on `div`; `cpuid` is
the control and must not move beyond its spread. Expected 20-30 ns on `div`. (Coldtrip does not measure this path: it
runs the literal exit, the linker, `system` and CPUID only.)

### Step 3: the trip path's code in one place (the footprint)

1. In `linker.ld`, an explicit first entry in `.text`, 16 KiB-aligned: `*(SORT(.text.trip.*))` before the existing
   `*(.text .text.*)`, which would otherwise take the trip sections in input order. The path order comes from the
   section names (`.text.trip.00_vectors`, `.text.trip.01_finish`, ...), not from the order of the attributes in the
   source. `gates.S`'s `.section .text` (`gates.S:45`) becomes `.text.trip.00_vectors` (the vectors keep `.balign 2048`).
2. `#[link_section = ".text.trip.NN_name"]` on the resident's functions a common trip runs: `finish`, `returned`,
   `sys_fast`, `linker`, `miss_fast`, `missed`, `bank`, `sse::bank`, `sse::fill`, `find_from` and `enter` (if out of line),
   `cache::{block_at, current_at, lookup, select_l1, fill_l1}`, `tables::select`, `copy_record`, `events::{none, take}`,
   `lapic::poll`, `post_pending`, `io_checkpoint`, `take_invalidations`, `resume`, `division`, `helpers::{x87, string}`,
   `aborted`, `access_aborted`. From the November 22 binary's sizes (experiment) about 25-32 KiB: two granules.
   Callees outside the list stay where they are: `system` (5.5 KiB, of which a trip runs a little), `natemu_abi`'s
   functions (`cpu::cpuid`, the walk), compiler builtins (`__udivti3`), libm. A `link_section` inside `natemu_abi` would
   break the host's Mach-O build (a section name there is `__TEXT,__name`), so any abi function moved is
   `cfg_attr(target_os = "none", link_section = ...)`; the first version moves none. Whether the attribute places every
   instance of the generic `cache::lookup` is unverified; if not, those instances are named in `linker.ld`.
3. The static `stack` case checks that every listed function lies inside the trip sections, in the named order, and
   that they total at most 32 KiB (`check_resident_stack.py` reads the disassembly and symbols already).

Kill-test: `guest-coldtrip-profile`'s `cold_ns` by the protocol, minimum 7 ns; `trip_small_ns` must not rise beyond its
spread. Expected at the low end of 0-10 ns: packing cuts instruction-TLB entries, not L1I lines (the code was already
hot in L1I at K = 1 and 2, section 3), and the TLB candidate is a guess. Coldtrip runs only the linker and `system` part
of the list; the misses' and helpers' code moves with it, unmeasured.

### Step 4, only if steps 1-3 leave the gate and footprint as the larger part: the linker in place

The literal exit's service on the record itself, as `sys_fast` does (no `load` at `execution.rs:710`, no write-back at
`:2312-2318`): chunk 22 estimated the record's copy out and back at about 24 ns per trip, and it is 2.8 KiB of data the
trip stops touching. Not lightweight: `find` can ask the host (`ask`'s write-out and reload around a host exit) and every
reader of `record()` during the path must be shown not to need the last full entry's values. Its own design note first;
kill-test `trip_small_ns`.

### Step 5, after step 0: helper trips removed

Whichever kind step 0 shows as most of configure's 14.1 M gets a design note for running it without a trip:
- PCMPxSTRx: lowered inline by fexc (the intermediate result is a pure function of two vectors, the control and the
  lengths: `natemu_abi::strings::pcmpstr`, `helpers.rs:33-39`), or a stack-free routine in the code area called from the
  block, as the dispatcher is copied there (`gates.S:253-266`). Generated code at EL0 has no usable stack (SP_EL0 points
  at an EL1-only page), so a code-area routine must use none.
- x87 transcendental and remainder instructions: they use the resident's libm today; inline lowering is a larger fexc
  change, so this branch is costed in its note before anything is built.

Kill-test: the kind's count in `io5-solo32-profile` (it must fall to near zero on configure) and a `k2-cost` row for the
instruction, added with the step. Estimate unverified: a 2-hour note, 6-12 hours to build.

## 6. Considered and left

- An assembly-only dispatcher-miss path that saves nothing (only x0-x2 are live at the miss, `gates.S:253-257`): the
  largest possible cut for 29 M misses, but `miss_fast`'s every check (debug, boundary, pending work, I/O window, LAPIC,
  events, uncached shadows: `execution.rs:973-1066`) would move into assembly. Not lightweight.
- `tables::select`'s third `isb` before the `eret`: an `eret` is a context synchronization event only when SCTLR_EL1.EOS
  is set or FEAT_ExS is absent (unverified here; the resident records SCTLR at its start, `main.rs:64-67`). Worth about
  10 ns per class change; it needs that premise checked at start and a system-call kill-test (`k2-cost`'s `syscall` row).
- Hot data in adjacent pages (`Shared`, the state record, the exception stack's top): an abi layout change. Its
  candidate effect is on configure's large guest data, which coldtrip cannot show; after step 3's result.
- The FPCR skip at the gate (E4): exact but a pure extra branch on AFP hosts (section 2.3).

## 7. Correctness

Exact means: at every fault and every resume, the guest's registers, flags and FP state, and the resident's own
invariants, are what they are today. Per item:

- **Frame contents.** Step 1.1 loads the same offsets into the same registers (`NativeFrame` layout, abi `lib.rs:52-60`;
  the checker's expected sequence, `check_resident_stack.py:240-244`, is rewritten to the pairs, with x16 last). Step 2
  writes the results (division's x0-x2, `string`'s x0) into the gate frame instead of `captured`, after reading every
  input; nothing reads the frame after `resume_exception` starts. Rust aliasing: the services get `frame_ptr` itself and
  form no `&NativeFrame` that outlives a write through it; `returned`'s `frame` is not used after the call.
- **SPSR and NZCV.** `returned` reads SPSR_EL1 itself, after `fp_cancel`, as today: an abandoned FP re-run resumes with SS
  and I cleared (`fp.rs:253-255`). Only ESR, FAR and ELR are passed (step 1.2); they cannot change before `returned`
  (interrupts masked, a nested EL1 fault halts, `execution.rs:2348-2350`). NZCV is stored from the gate's SPSR read as
  today (`gates.S:101-103, 154-156`); abandon changes no flag bit. Every resume builds PSTATE as today (`BLOCK_SPSR |
  nzcv`, `execution.rs:2321, 1062`; the frame's SPSR for `resume`).
- **FPCR and FPSR.** The resident still runs under FPCR 0 (I5a): the save's `msr fpcr, xzr` stays unconditional. Step
  1.4, if built, skips only a write of the value FPSR already holds. `fp::cancel` still edits the frame's FPCR and FPSR
  before anything reads them (`main.rs:95`, `fp.rs:250-251`); with step 2, FP traps keep their own copy (`fp.rs:173-178`).
- **SP_EL0 and SPSel.** SP_EL0 is saved and restored as today. SPSel (step 1.3, if built): the only writes are
  `gates.S:40` (boot, to 0) and `:250`; the vCPU starts at EL1h (`host/src/vm.rs:490`); written whenever it reads 0, so
  the boot's first entry switches as today. The stack checker's switch for `enter_allocated`
  (`check_resident_stack.py:52-54`) stays, now conditional.
- **The checker's conditional branches.** Its `skip` (`check_resident_stack.py:245`) replaces a `cbz`/`tbz` target with
  `SKIP` without checking where it goes. Every new conditional skip (1.3, 1.4) gets a target check: the branch must land
  on the instruction right after the skipped write.
- **Alignment checking.** Unchanged: the gate clears SCTLR_EL1.A before resident code (`gates.S:87-94`), the resume sets
  it from x3 (`gates.S:169-176`); every resume still passes `alignment_check` (`execution.rs:2494, 2511, 1063`).
- **Guest space selection.** Unchanged: `select_guest_space` before every resume (`execution.rs:2492, 2509, 1054, 2319`).
- **Stop arming.** `resume` still arms a wanted stop in the block holding the PC (`execution.rs:2485-2490`); step 2's
  in-place record gives it the same `stop_wanted`.
- **`shadow_entry`.** Written only by `enter` and `miss_fast` (`execution.rs:2131-2135, 2306-2308, 1049`); no step
  changes either.
- **Interrupts.** The resident runs with IRQs masked (an exception entry sets PSTATE.I). The IRQ gate passes the same
  registers; `returned` reads ESR only for kind 1 (`execution.rs:2337-2347`, `finish`'s `fault == 1` test).
- **The stack bound** (static `stack` and `stack-profile`, contract 62): step 1 adds register arguments only; step 2
  removes a state copy from three frames; step 3 moves code, not frames. The ERET audit's expected save and restore
  sequences (`check_resident_stack.py:240-244, 272-279`) are updated with the change, in the same commit.

Families that cover each path (the affected tier runs them; the gate runs everything before the pooled merge):

| Path | Families and cases |
| --- | --- |
| The gate and the return (every kind) | static `stack`, `stack-profile`; programs (`fix`, `seed`, `i9`, `i10`, `rex`, `i7`, `xsave`: registers, XMM, flags across blocks); system `k1-*`, `k2-*`; boot `boot-smoke`, `boot-i386`; correctness `x86st`, `torture`, `i386` |
| Literal exits, misses, `sys_fast` | system `k1-*`, `k2-sysfast`, `k2-swapgs`; smp `events`, `delivery`, `contain`, `privilege` |
| FPCR, FPSR and the FP re-run's SPSR | faults `i5c`, `i5a-de-*`, `i6-faults`, `i8-faults`; fp `i8`, `i8b`, `approx-special-accurate`, `approx-special-fast`; unit `rust-afp` |
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
| T3 | 1.2 | `returned` given FAR in ELR's place | any K1 program (every exit halts `bad_link`) |
| T4 | 1.2 | `returned` uses an SPSR read in the gate instead of its own | fp `approx-special-accurate` on a host without AFP (the M1), where an abandoned re-run then resumes with SS and I set (unverified which case turns red: the implementer records it); on AFP hosts the path is not taken, and the mutation is documented as equivalent there |
| T5 | 2 | division writes its quotient to `Shared::captured` and resumes from the gate frame | `k2-cost` `div` (its R8 check sees a wrong loop count) and a programs case with a 128/64 division |
| T6 | 2 | `finish` skips the copy for `svc #X87` while `helpers::x87` still resumes from `captured` | programs `i6b` |
| T7 | 1.3 | the SPSel test reads the wrong bit (never writes) | static `stack` (a checker unit test; at run time SPSel is already 1 after the first exception, so a run would stay green) |
| T8 | 1.3, 1.4 | a conditional skip whose target is past the next write | static `stack` (the new target check) |
| T9 | 1.4 | the FPSR skip inverted (write only when equal) | faults `i5c` (MXCSR flags from FPSR) |
| T10 | 3 | `#[link_section]` removed from `returned`, or two trip sections' names swapped | static `stack` (the placement and order check) |
| T11 | 0 | the X87 counter incremented in the STRING arm | a check that runs programs `i6b` under the perf resident and compares each kind's count with the number of x87 and PCMPxSTRx helper instructions the programs execute (the sum alone would stay right) |

## 9. Estimate (agent hours)

| Step | Work | Hours |
| --- | --- | --- |
| 0 | the helper split counter, its host report, T11 | 1 |
| X1 | the floor experiment, three variants | 2.5 |
| 1 | 1.1 and 1.2, the checker's sequences and target checks, T1-T4, T7-T9 as built; kill-test runs | 3 |
| 2 | the three services in place, `frame_only`, T5, T6; the aliasing audit; kill-test runs | 3 |
| 3 | the trip sections, the attributes, the placement and order check, T10; kill-test runs | 3.5 |
| Reviews | an independent review of each step's diff (AGENTS.md: authors and reviewers separate) | 2 |
| 5 | the helper design note (after step 0); then building it | 2, then 6-12 |
| 4 (conditional) | the linker in place: a design note first | 6-8 |

Steps 0-3, X1 and reviews: about 15 hours, plus machine time for 12 coldtrip runs (steps 1 and 3), 3 for X1 and 6
`k2-cost` runs (step 2).

## 10. Expected gain, and why

- Step 1 (1.1 and 1.2): about 1-2 ns per trip, every trip: configure 65.2 M trips, 0.07-0.13 s of its 30.35 vCPU-s guest
  bucket (0.2-0.4%). It may not clear its kill-test. 1.3 and 1.4 add only what X1 shows.
- Step 2: 20-30 ns per helper or division trip (10-15 in the guest bucket, the copy; 10-15 in the helper bucket, the
  record). Configure's 14.1 M helpers: 0.28-0.42 s.
- Step 3: the low end of 0-10 ns of cold state per trip: configure 0-0.65 s (0-2% of its guest bucket).
- Steps 1-3 together, configure: about 0.35-1.2 s, 1-4% of its guest bucket, against the 19.5% (16.3-22.9) of gate plus
  cold state today. The gap is the floor (section 2.1) and the two FPCR writes, which X1 prices.
- Step 5: up to about 3.1 s on configure if the dominant helper kind is most of the 14.1 M (each trip removed saves its
  whole 220 ns or so): the largest item in this plan, sized by step 0.

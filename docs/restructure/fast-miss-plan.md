# Fast miss path: a dispatcher miss served from the exit frame, like `sys_fast`

Status: design, not built (November 14, 2026), branch `perf-real-workload`; roadmap item 2 of the
[architecture review](architecture-review-20261113.md) (root cause B; decision 11 of [io5-perf-notes](io5-perf-notes.md):
about 60% of a current-block miss is state copying and barriers the miss never needs). Built right after the
[page-currency plan](page-currency-plan.md), whose record turns the "needs revalidation" misses (54.4% of configure's)
into misses this path can take. Line numbers are those of `bcc164f`; buckets from `natemu-raw/group-r1-profile-2`
(the current baseline, sums over four vCPUs).

## 1. What a dispatcher miss costs today

A computed exit (`ret`, indirect `call`/`jmp`) branches to the dispatcher (`gates.S:247-253`): `and x1, x0, L1_MASK`,
`adrp`, `add`, `br` into the RIP's L1 entry; an entry that is empty or holds another RIP branches back to the
dispatcher's last word, `svc #MISS`. From there:

1. **The gate** (`exception_capture`, `gates.S:96-106`; `save_native`, `:45-94`): 31 GPRs, 32 Q registers, FPCR, FPSR
   and SP_EL0 saved on the exception stack, SCTLR_EL1.A cleared if set; `finish` (`main.rs:76-90`) skips the 800-byte
   copy into `Shared::captured` for `svc #MISS` since chunk 22 (`frame_only`, `execution.rs:1739-1743`). Charged to
   `guest` (before the bucket switch): unavoidable, unchanged by this plan.
2. **`returned`** (`:1680-1736`): ESR, FAR, ELR, SPSR; the perf bucket; `cache::disarm` (one load when nothing is
   armed); the watch-step cancel; the `svc` dispatch to `missed`.
3. **`missed`** (`:1747-1766`): `load()` copies the whole state record to the stack (`read_record`, `record.rs:33`);
   `bank` (`:2663-2701`) rebuilds 16 GPRs, RFLAGS (NZCV, PF/AF registers, DF and, at CPL 0, IF from the frame page),
   GS.base and IA32_KERNEL_GS_BASE at CPL 0, XMM0-15 and MXCSR's flags; `completed` (RF); `rip = x0`.
4. **`find`** (`:167-260`): `io_checkpoint`, `take_invalidations` (a drain), `lapic::poll`, the debug tests, `events::take`,
   the breakpoint tests, `select_l1`, then `lookup` (bucket `lookup`: the L2 probe and `current`) and, for a block no
   longer current, the revalidation walk (bucket `revalidate`).
5. **`enter`** (`:1480-1674`): `uncached_end`, `served`, the shadow bookkeeping, about fifteen frame-page writes (eleven
   flag bytes, FS/GS bases, KERNEL_GS_BASE, CR2, the VERW GDT pair, FAST_STRINGS, FCW, MXCSR, TSC_OFFSET, the SIMD and
   x87 pending bytes, EVENT_PENDING), the entry frame rebuilt from the bank (16 GPRs, PF/AF, x28, `sse::fill`), the state
   record written back (`enter_copy`), `tables::select`, then `enter_allocated` -> `resume_exception` -> `eret`.

| Bucket (per event) | configure | make -j4 | Of which a miss pays |
| --- | --- | --- | --- |
| `miss` (steps 2-4 but the lookup) | 7.06 s / 32.0 M = 220 ns | 41.55 s / 131.9 M = 315 ns | all |
| `lookup` | 2.69 s / 98.0 M = 27 ns | 9.34 s / 270 M = 35 ns | one probe per miss (the rest are the linker's) |
| `revalidate` | 2.43 s / 23.1 M = 105 ns | 6.37 s / 52.2 M = 122 ns | the 54.4% / 29.0% of misses whose block needs it |
| `enter` + `enter_copy` | 3.65 + 0.83 s / 62.0 M = 73 ns | 12.96 + 2.99 s / 199 M = 80 ns | one per miss (misses are 32.0 M / 131.9 M of the entries) |

So a miss into a current block costs about 220 + 27 + 73 = **320 ns** on configure and 315 + 35 + 80 = **430 ns** on
make, plus the gate; a miss whose block needs revalidation about 105-122 ns more. By the census (decision 15), misses
are: L1 entry held another RIP (the block is current) 17.7% / 66.3%; needs revalidation 54.4% / 29.0%; needs
translation 27.8% / 4.6%; L1 empty about 0.

What the miss *needs*: to find the block for (RIP, context) and enter it as the L1 entry would have, with an L1 refill.
Nothing in the guest state changes between the exit and the entry: no instruction runs, so the registers, NZCV, the
PF/AF registers, FPCR/FPSR, the frame page's live bytes (DF, IF, the GS bases, the SIMD and x87 pending flags) and the
CPL are exactly what the target expects. Steps 3 and 5 copy that state out into the record and back into a new frame;
step 4 re-checks a boundary at which, in the common case, nothing is pending. That is the 60%.

## 2. Design

### 2.1 `miss_fast`, before `missed`

`returned` calls `miss_fast(frame, elr)` for `svc::MISS` (at the dispatcher's miss word) before `missed`, as it calls
`sys_fast` before `linker` (`:1724-1728`). Like `sys_fast` it works on the state record in place (`&mut *record()`), is
`#[inline(never)]` and holds no `State` copy on its frame (the nested-entry stack bound). It either enters the target
block or returns having written nothing but perf counters, and `missed` runs as today.

**Checks, all reads** (the record, `Control`, the shared words), in this order, each a decline reason counted by a
plain increment of a per-reason counter (not as `sys_fast`'s `declined`, which probes `kept_at` first, `:765-773`):

1. Debug: `s.debug != 0`, RFLAGS.TF, DR7 enables (`:697`'s test in `sys_fast`).
2. Boundary state: `s.cpu.mov_ss | s.sti_shadow | s.mid_string | s.plain_step | s.stop_wanted != 0`. This covers the
   step block's computed exit, which goes to the dispatcher's miss on purpose (X2: the boundary after a stepped
   instruction is where a held event is taken, `cache.rs:1312-1316`): after a step `plain_step` or `debug::STEP` is set,
   and an event a shadow held is seen by check 4; so the X2 contract is kept by checks 1, 2, 4 and 5 together.
3. Pending work: `cache::pending()` (posted pages, a flush or shadows request), `io_pending(s)` (an I/O window),
   `lapic::due()` (an expiry due, or a reinjection backlog: both make `poll` do work).
4. Events: `events::none` reads IF from `s.cpu.rflags` (`events.rs:69`). At CPL 0 the live IF is the frame page's byte
   (`frame::IF`, what `bank` reads, `:2675-2681`), so the fast path first writes that bit into the record's `rflags` (one
   masked store; the field is stale anyway and the next full `bank` rewrites it), then calls `events::none` unchanged.
   At CPL 3 the record's IF is the live one. A takeable event declines.
5. Uncached shadows: `Control::uncached_count | uncached_fence != 0` (`uncached_end` would unmap, `:384-399`).
6. Test and study builds: `Control::pic_test != 0` declines; `pic-count` and `dispatch-study` builds decline before the
   target probe so their miss classification (`missed`, `:1752-1764`) keeps counting every miss; the `io-test-hooks`
   build reports its point (2.4) and then runs the real checks.
7. The target: `context = s.cpu.code_context()` (generated code changes none of its inputs: CPL, the control registers,
   the MXCSR and FCW masks, XCR0, RFLAGS.AC, DR7, as `sys_fast`'s contract states, `:660-662`); `cache::select_l1(context)`;
   `cache::current_at(rip, context)` with `rip = frame.x[0]`. No block, a `SYSTEM` decode (the full path executes it) or a
   `STEP` block declines. An `STI_FIRST` target is entered as an L1 hit enters it (the flag concerns the linker's refusal
   to link a block's exit to its own start, not entries). Chunk 2 adds the page-record revalidation (2.3).

**Commit**, all writes resident-side: `s.cpu.rflags &= !RF` (`completed`'s effect; `stepped` is a no-op with `debug`
0); `s.shadow_entry = 0` (what `enter` writes with no shadow, `:1489-1493`); `s.blocks += 1`, `s.l2_hits += 1`, the
taken count; `cache::fill_l1(rip, target)` (the L1 refill with its code synchronization, as today; item 1's IDC gating
shortens it); `tables::select(s.cpu.cpl)` (returns at once: the CPL is the exit's); `perf::enter(GUEST)`; then
`resume_exception(frame, pc, BLOCK_SPSR | frame.nzcv, alignment_check(&s.cpu))` with `pc =
cache::code_of(target).full_entry()` (PSTATE built as `enter` builds it, `:1672`, from the NZCV the gate stored).

**Why the exit frame is the entry frame.** An L1 hit enters `full_entry` with x0 = RIP and every other register as
the exit left it (`gates.S:239-253`; `cache::l1_words`). The gate's frame holds exactly that state: x0 is the RIP, the
static registers the guest GPRs, the PF/AF registers, the Q registers, FPCR/FPSR and the exit's NZCV in SPSR. For a
`PIC` target `full_entry` is the `and x0 / str x0, [x28, #PAGE_BASE]` prologue (`Block::full_entry`, abi
`execution.rs:817-823`), which writes the page base itself, so the fast path writes no frame-page field at all. The
frame lies on the exception stack, below its top; `resume_exception` (`gates.S:161-229`) resets SP to that top and then
only loads from the frame before `eret`. **This is the first path that resumes from the gate frame itself**: `division`,
`interrupted` and the helpers resume from the copy in `Shared::captured`, and `enter` from `Shared::initial`. So the
layout becomes a contract: nothing between the SP reset and the `eret` may push (a push would silently overwrite the
frame's `sp`/`nzcv` fields, not fault), and chunk 1 adds a comment at `resume_exception` stating it, with the
fast-miss caller named. A review point, since no test sees it directly.

### 2.2 The invariant this rests on, and what stays stale

**The state record is authoritative only at a full entry, after `bank`; while generated code runs, the registers and
the frame page are.** The precedent is the L1 hit and the patched link: a block entered through either runs with the
record exactly as the last full entry left it, for as long as linked and L1 transfers go on. **After a fast miss the
record and the frame page are what an L1 hit from the last full entry leaves them, plus RF cleared and `shadow_entry`
= 0.** Every full entry banks before it reads guest state (`missed` `:1749`, `linker` `:563`, `sys_fast` `:708`,
`interrupted`'s `at_once` `:1870`, `stopped`, `aborted` from the site) and every one computes RIP from the frame or the
faulting site, never from the record (`:564`, `:1751`, `:1871`, `:2796`). The reviewer checked the readers reachable
before a bank: `fp_exception`'s MXCSR/FPSR pairing, `bank_at`, `halt`'s diagnostics and the resident-owned controls;
none reads stale guest state. The implementer's grep of every reader of `s.cpu.*` reachable without a preceding
`bank` confirms that list at build time, and the review signs it.

Frame-page fields `enter` rewrites and why none needs it here: the flag bytes other than DF/IF, FS.base, FCW, MXCSR's
value, TSC_OFFSET, FAST_STRINGS change only in system instructions or helpers (full path); DF, IF, GS.base,
KERNEL_GS_BASE, the SIMD and x87 pending bytes are live in the page (generated code writes them, `bank` reads them);
CR2 and the VERW GDT pair are the CPL's and the CPL is unchanged; EVENT_PENDING is maintained by every writer of event
state (the last `enter`, the doorbell's `interrupted`, `:2704-2718`) and no event state changed; PAGE_BASE is written by
the PIC prologue entered. `alignment_check` reads CR0.AM, CPL and RFLAGS.AC from the record: none is written by code
(AC's byte is the bank's own, `:2658-2660`).

### 2.3 Interaction with page currency (chunk 2)

With the page record, a miss whose block is not current but whose pages are recorded with its frames needs no walk, so
no fault, no A-bit write, no code-page kill and no host request can follow: the only reasons the full path's `find`
exists for such a miss. Chunk 2 calls `cache::lookup(rip, context, record_only)` where `record_only` answers from the
page records alone and has a third answer: `Mapped::Yes(flags)`, `Mapped::No` (a record for every page, one frame
differs: `remapped`, as a walk would say), `Mapped::Unknown` (some page has no record). `lookup` ends its search on
`Unknown` and the fast path declines (the full path walks). `lookup`'s side effects on `Yes` (`checked`, flags,
`mark_large`, the instance-with-global cut, and the relink plan's re-patching, all resident-side) are fine here: none
reads the bank. The page plan's `sb` under the barrier switch runs inside the probe.

### 2.4 Interrupts, faults, hooks, barriers

- **Interrupts.** The resident runs with IRQs masked (`BLOCK_SPSR`'s D/A/F and the EL1 mask). A doorbell or timer
  arriving during the fast path is taken at the `eret`, at the target's first word (`irq_gate` -> `interrupted`, which
  banks from that frame and stops at once at a block's entry state, `:1836-1875`): exactly what an L1 hit followed by
  the same interrupt would do, and what `sys_fast`'s argument relies on (`:653-657`).
- **Faults.** The fast path reads and writes only resident memory: the record, `Control`, the L2 table, block records,
  the L1 code area through its write alias (`fill_l1`), the page-record table (chunk 2). No guest memory, no `hvc`. A
  resident fault halts as anywhere (`resident_fault`).
- **Stops, steps, hooks.** TF, `debug`, DR7, `plain_step`, `stop_wanted` and `pic_test` decline (2.1); the
  `io-test-hooks` build reports `MISS_FAST_CHECK` and then runs the real checks, so the hook cases of section 5 see the
  path's own decisions. The host's stop RIP never has a block, so a miss there takes the full path. `--check-reuse` is
  unaffected (no placement). `link-check` is unaffected (no link made).
- **Speculation barriers.** The path issues no walk and loads nothing from guest memory; it leaves through `eret`.
  The walk's `sb` is the full path's; chunk 2 inherits the page plan's barrier under the switch.
- **Relink** (relink plan): runs inside `lookup` after `checked` is written, resident-side, halting if `armed != 0`;
  `disarm` ran in `returned`. Unchanged on this path.
- **Capacity.** No placement, so no flush can happen here; a flush request pending (`cache::pending`) declines.

## 3. Cost and gain (estimates; the per-miss costs are measured in chunk 1)

Fast path, in the `miss` bucket: `returned`'s prologue about 15 ns; the checks about 15-20 ns (a dozen loads, one
atomic); `select_l1` about 5 ns (90 ns on a context switch, on both paths); the L2 probe 27-35 ns (bucket `lookup`,
unchanged); `fill_l1` about 40-60 ns (a 32-byte write and `dc cvau`, `dsb`, `ic ivau`, `dsb`, `isb`, `cache.rs:1369-1387`;
less with item 1); the commit's stores about 5 ns. About **110-150 ns** against today's 320 / 430 ns: a saving of about
**0.13-0.18 us per taken miss on configure, 0.18-0.25 us on make**. The gate's save and restore stay in `guest`.

**A declined miss costs more than today**: the checks, `select_l1` and the L2 probe (about 45-50 ns) run, then `find`
repeats the probe in `lookup`. Declines are every miss whose block is not current (chunk 1) or has no complete page
record (chunk 2), plus the rare boundary declines.

**Make's 315 ns against configure's 220 ns per miss** is not explained by the probe alone: part of it may be drain and
checkpoint work charged to `miss` on misses that would decline. Chunk 1 therefore measures the saving per *taken* miss
directly (CNTVCT around the fast path, and around a matched sample of full-path misses into current blocks) before the
0.18-0.25 us figure is trusted.

Eligible misses (census shares; 95% assumed taken after boundary declines, which `sys_fast` suggests: 0.002% on
`hackbench`):
- Chunk 1 (current blocks): configure 17.7% x 32.0 M = 5.7 M taken, 26.3 M declined; make 66.3% x 131.9 M = 87.5 M
  taken, 44.4 M declined.
- Chunk 2 (plus page-record hits at the record's 89-91% hit rate): configure + 0.544 x 0.9 x 32.0 M = 15.7 M, 21.4 M
  in all (67%), 10.6 M declined; make + 0.29 x 0.91 x 131.9 M = 34.8 M, 122 M in all (93%), 9.6 M declined.

| | configure (109.5 busy vCPU-s; serial) | make -j4 (561.5; wall / 4.1) |
| --- | --- | --- |
| Chunk 1 gain | 5.7 M x 0.95 x 0.13-0.18 us = 0.7-1.0 s | 87.5 M x 0.95 x 0.18-0.25 = 15-21 vCPU-s |
| Chunk 1 decline cost | 26.3 M x 0.045-0.05 = 1.2-1.3 s | 44.4 M x 0.045-0.05 = 2.0-2.2 vCPU-s |
| **Chunk 1 net** | **-0.6 to -0.2 s: zero or a small loss** | **13-19 vCPU-s** (2.3-3.4%; about 3-4.6 s wall) |
| Chunk 2 adds | 15.7 M x 0.95 x 0.13-0.18 = 1.9-2.7 s, declines fall to 10.6 M x 0.05 = 0.5 s | 34.8 M x 0.95 x 0.18-0.25 = 6-8 vCPU-s, declines 0.5 |
| **Chunks 1 + 2 net** | **2.1-3.2 s** (1.9-2.9%) | **21-29 vCPU-s** (3.7-5.2%; about 5-7 s wall) |

So on configure chunk 1 alone is at best neutral and is gated on "no regression", not a gain; configure's gain comes
with chunk 2. The review's figures were 6.4 s / 26 vCPU-s; configure is lower here because its 27.8% "needs
translation" misses can never be fast, declines are costed, and the saving per miss is taken from the measured buckets
rather than 0.2 us flat. The revalidation walks chunk 2's misses also skip are the page plan's gain, counted there.
Make's chunk 1 pool (the 87.5 M L1-conflict misses) is also what a larger or 2-way L1 would remove outright; section 7
compares the two and sets the build order.

A way to cut the decline cost, kept in reserve: hand `find` the L2 slot the fast path probed, so `lookup` starts
there instead of hashing again (saves the repeated probe, about 30 ns per decline). Rejected for now as plumbing
through `find`'s loop; taken up only if chunk 1 regresses configure.

## 4. Rejected

1. **Copying the gate frame into `Shared::initial` and going through `enter`**: an 800-byte copy and every frame-page
   write for nothing; the exit frame already is the entry frame (2.1).
2. **A partial bank** (GPRs only, or flags only): nothing on the path reads guest registers; a partial copy keeps the
   cost and loses the clarity of "the record is stale until the next full entry".
3. **Skipping `fill_l1`** (enter the block and let the next miss refill): the L1 entry is the point; a miss that does not
   refill makes every later computed exit to that RIP a miss.
4. **Taking `SYSTEM` targets** (run the kept decode in place, as `sys_fast` runs SYSCALL): the general transaction
   needs the bank and may fault or exit to the host; `sys_fast` covers the two frequent ones already.
5. **Walking on the fast path** when the page record misses: a walk may fault (delivery needs the bank and RIP), write
   an A bit into a page holding translations (killing blocks, maybe the one that exited) and run the observer; the full
   path exists for that. The page record is what makes the common case walk-free (2.3).
6. **Handling the "needs translation" misses** (a host request without the bank): the host reads the record (`ask`
   writes it, `:830`), so the request needs the full state; and placement is 4-5 us of host time against 0.3 us saved.

## 5. Chunks, gates, tests, mutations

The eligible shares are already measured (decision 15), so no counting chunk: chunk 1 carries decline counters by
reason (as `sys_fast_declined`) and a `miss_fast` count, and its profile reports the taken share.

**Chunk 1: `miss_fast` for current blocks (M: `execution.rs` only, about 120-180 lines plus the hook point; an
independent review before the gate, with the stale-state grep of 2.2 as a named review item).**
- Tier 0: resident checkers (stack bound: no `State` on the fast path's frame); the mutations below each run once.
- **Which misses chunk 1 takes.** Only a miss whose target is current but not in L1: an L1 entry taken by another RIP
  with the same low 16 bits (the table is direct-mapped on `rip & L1_MASK`) or a table lost to another context. A
  block's *first* miss in an epoch goes through `find`, which fills L1 itself (`:368`), so a program that merely calls
  a block never takes the fast path; and most existing K1 misses are into non-current blocks (after a CR3 load or an
  INVLPG), which decline. Every fast-path test therefore (1) warms the target V in the present epoch, (2) evicts its L1
  entry by running a block at V + 64 KiB (`L1_BITS` = 16: the same entry) in the same code context, (3) jumps to V by
  `jmp r13`. The `miss_fast` count asserts the path was taken.
- Tier 1: every `run_k1.py` program at 1 and 4 vCPUs (for the currency rule: `cr3-switch`, `remap-cr3`, `remap-invlpg`,
  `cross`, `global`, `place-global`, `contexts`, `local-write`, the `reuse-*` and PIC programs, all `jmp r13`; most of
  their misses decline, which the counters show); `link-check`; `run_k2.py` (the `sysfast` cases still run `sys_fast`);
  `run_slice3_vm.py events delivery smp coherence`; `run_x3.py` (timers); `run_s4.py` (debug); the X2 shadow and #AC
  tests; `--check-reuse` boot with 0 mismatches. New short programs (K1 stays at its length), each with the warm/evict
  prelude above:
  - `miss-counts` (natemu only, the `counts` form): warm A; run B = A + 64 KiB (evicts A); `jmp r13` to A (a fast miss:
    `miss_fast` 1); `jmp r13` to A again (an L1 hit: misses unchanged). Then K rounds of A, B, A, B: misses per round 2,
    `miss_fast` per round 2, `revalidated` 0.
  - `miss-step` (QEMU): warm V, evict; TF set by `popf`; `jmp r13` to V: the single-step #DB is delivered after V's
    first instruction (the handler records RIP). Second part, TF clear: DR0 as an instruction breakpoint at V, warm and
    evict with DR7 armed throughout (the code context includes DR7, so the warm block is in the armed context's table):
    #DB before V's first instruction.
  - `miss-ac` (QEMU): CR0.AM and RFLAGS.AC at CPL 3 throughout (AC is part of the code context, abi `execution.rs:1851`,
    so warm and evict happen in that context): warm V, evict, `jmp r13` to V, whose first instruction is a misaligned
    load: #AC (the resume must set SCTLR_EL1.A).
  - `miss-pic` (natemu only): a shared translation's instance I at an ASLR alias (as `reuse-alias`): warm I, evict with a
    block at I + 64 KiB in the same context, `jmp r13` to I: its `lea [rel]` value is the alias's (the entry is
    `full_entry`, which writes the page base).
  - `miss-system` (natemu only): a RIP holding a kept CPUID decode, reached by `jmp r13` after a warm and evict: the
    decode is executed (the full path) and the next block reached (values and the `systems` count).
  - Hook cases (`io-test-hooks` build, `run_io_window_cases.py`, a new point `MISS_FAST_CHECK` reported before the
    checks; the build runs the real checks after the point, so mutants differ). The host kicks unacknowledged vCPUs
    (host `execution/io.rs:448`), and a kick's interrupt taken at the `eret` would hide a mutant that skipped a check, so
    at this point the harness **posts without ringing the doorbell**, and each case asserts the per-reason decline
    counter as well as the outcome: (a) an I/O window published at the point: `declined[io]` 1 and the window
    acknowledged before any guest access; (b) a code-page post for the target's page at the point (no kick):
    `declined[pending]` 1 and the new bytes run; (c) an interrupt posted at the point with the record's IF 0 and the
    page's IF 1 (the block entered with IF clear executed an inline `sti` before its `jmp r13`): `declined[event]` 1 and
    the interrupt delivered at V's first instruction (the handler records RIP = V). A mutant reading IF from the
    record sees 0, takes the path, and delivers late.
- Tier 2 gate: on make, `miss` + the miss share of `enter` + `enter_copy` down by at least 60% of the chunk's low end
  (section 3: at least 8 vCPU-s net of declines; recomputed on the remaining conflict misses if the L1 item of section 7
  lands first), `lookup` and `link` unchanged within noise, the taken share at least 60% of current-block misses; on
  configure, **no regression** (busy time within the spread). Wall time judged in group R (items 1-3), with the page
  plan if measured together.

**Chunk 2: page-record hits** (after the page plan's chunk 1): `Mapped::Unknown` in `lookup`, `record_only` on the fast
path. Gate: the taken share at least 60% of all misses on configure and `miss` down by at least 60% of chunk 2's low
end (1.9 s configure, 6 vCPU-s make); the page plan's K1 programs (all computed jumps) green; `page-counts` shows the
hits now taken by the fast path (`miss_fast` counts).

**Deliberate mutations, each turning its test red:**

| Mutation | Caught by |
| --- | --- |
| takes a block that is not current (`current_at` -> `kept_at`) | `cr3-switch`, `remap-cr3`, `remap-invlpg`, `page-share` (a) |
| enters at `entry()` instead of `full_entry()` | `miss-pic` (the instance runs with the wrong page base) |
| ignores TF or `debug` | `miss-step` first part, `run_s4.py` |
| ignores DR7 | `miss-step` second part |
| skips `fill_l1` | `miss-counts` (the second jump misses again) |
| skips `io_pending` | hook case (a): counter 0, window crossed |
| skips `cache::pending` | hook case (b): counter 0, old bytes |
| skips `events::none`, or reads IF from the record instead of the page at CPL 0 | hook case (c): counter 0, late delivery |
| resumes without SCTLR_EL1.A | `miss-ac` |
| PSTATE from the gate's SPSR instead of `BLOCK_SPSR | nzcv` | the resident checkers (SPSR.M/DAIF of generated code), `miss-counts` values |
| takes a `SYSTEM` target as a block | `miss-system` (halt or wrong values) |
| ignores `uncached_count` | the M2 UC programs (`run_slice3_vm.py`'s uncached cases) |
| ignores `mov_ss`/`sti_shadow`/`mid_string` | the X2 shadow and REP tests (`events`, K1 `string` cases) |
| chunk 2: `Unknown` treated as `Yes` | `page-share` (a), `page-cross` |
| a `State` copy on the fast path's frame | the stack-bound checker |

## 6. Stop rule and risks

- Drop chunk 1 (counters kept) if make's gate is missed after one round of diagnosis, or if any suite above goes red
  for a reason traced to the design (the stale-record invariant of 2.2) rather than a slip; any `--check-reuse`
  mismatch or `BAD_LINK` traced to the path stops it for re-review. Chunk 2 is dropped alone if its taken share stays
  under 60% on configure.
- Risks: the stale-record invariant has readers the grep must confirm (the reviewer's list in 2.2; a path that
  resumes mid-block and reads `s.cpu.rip` from the record would be a latent bug today as well); the decline rate on
  make may be higher than `sys_fast`'s if posted invalidations or windows often coincide with misses (counted); the
  decline cost makes chunk 1 neutral at best on configure (section 3); `fill_l1`'s synchronization is the largest
  remaining term and depends on item 1 landing first; the gate frame's reuse under `resume_exception` is a new contract
  on the exception stack (2.1): a push between the SP reset and the `eret` would silently overwrite the frame's `sp` and
  `nzcv` fields, so it is guarded by the comment and the review, not by a test; the make gate depends on the build order
  of section 7.

## 7. Make's conflict misses: fast miss path or a bigger or 2-way L1, and the build order

Chunk 1's make pool is the 87.5 M misses (66.3%) whose L1 entry held another RIP (census decision 15). A larger or
2-way L1 would remove most of those misses outright, trap and all, and decision 15 kept that item open for make
("conflicts, not hash quality": decision 9's XOR fold changed the index and gained nothing). The two attack the same
events, so one order has to be chosen.

**How the L1 works** (`gates.S:247-253`; `cache::l1_words`, `cache.rs:303-315`; abi `area`, `execution.rs:245-257`).
The dispatcher is `and x1, x0, #L1_MASK; adrp x2; add x2, x2, x1, lsl #5; br x2`: four instructions, data-free. An
entry is 32 bytes of code: `movz/movk x2` x 4 (the RIP), `sub x2, x2, x0`, `cbnz x2, +8`, `b entry`, `b miss`. Four
tables (one per code context) of 2^16 entries, 2 MiB each, 8 MiB of the 128 MiB code area per vCPU, with owner words
and the CR3 lists beside them in the lookup area. A hit costs the dispatcher's four instructions plus the entry's
seven: about 2-4 ns. A conflict is two live RIPs with the same low 16 bits in one context.

| Option | Cost per event | What limits it | Estimated effect on make's 87.5 M conflict misses |
| --- | --- | --- | --- |
| (i) fast miss path (this plan) | a taken miss about 110-150 ns plus the gate (about 70 ns in `guest`) instead of about 500 ns; a decline +45-50 ns | nothing structural; the trap stays | about 0.2 us saved each: **13-19 vCPU-s net** (section 3) |
| (ii) larger direct-mapped L1, `L1_BITS` 16 -> 17 or 18 | hit unchanged (the mask is one `and` immediate for any contiguous bit range; `adrp` reaches it); a conflict that no longer happens saves the whole trip, about 0.5 us; `clear_l1` (a global flush or a table given to a new context, the `l1` bucket: 0.23 s / 986 events = 235 us per 2 MiB table) grows with the table: about +0.25 s per doubling on make | **memory in the code area**: 8 -> 16 -> 32 MiB of the 128 MiB that blocks share (the code area is the capacity limit today: 9-11 capacity flushes per run, each about 2-2.5 vCPU-s by the review's estimate, so 24 MiB less block space at 18 bits may add 1-2 flushes, about 2-5 vCPU-s); the owner and list arrays (4 bytes per entry per table) double with it; more instruction-TLB and cache footprint for the sparse hot entries (chunk 19's 16 -> 64 Ki was a net gain: `build` -1.1 s) | a fraction f of conflicts removed, unknown: with the same-set pairs that chunk 19 found dominant, 17 bits may remove half and 18 bits most; f x 87.5 M x 0.5 us = **22 (f = 0.5) to 35 (f = 0.8) vCPU-s**, less the clear cost and 0-5 vCPU-s of extra flushes at 18 bits |
| (iii) 2-way L1 at 2^15 sets x 2 ways (the same 2 MiB per table) | a way-0 hit unchanged; a way-1 hit +7 instructions (the way-0 entry's `b miss` becomes `b way1`, which compares and enters or branches to the miss): about +2-3 ns, on the way-1 hits only, not on every hit; a miss +7 instructions; the fill chooses the way whose owner is empty or not current, else the older (one bit per set) | no extra memory; the entry and the lists address sets, so `fill_l1`, `empty_l1`, `tlb`'s list pass and `clear_l1` change (M effort against (ii)'s S); conflicts of three or more hot RIPs per set remain | the same-set pairs removed, chains of three not: f likely between 17 and 18 bits' at 16 bits' memory: about **22-30 vCPU-s** |

Configure: its conflict misses are 5.7 M, worth 5.7 M x 0.5 us x f = **1.1-2.3 s** for (ii) or (iii), against (i)'s
neutral chunk 1; configure's miss problem is revalidation, which only chunk 2 (page records) addresses.

**Recommended order.**
1. **Count first (S, one scratch profile, scheduled with the next census-style run):** shadow owner tables at 17 and
   18 bits direct-mapped and at 2-way 2^15 sets, kept beside the real L1 (owners only, 1-4 MiB each), counting for
   every real conflict miss whether each shadow table would have hit; plus the distribution of live RIPs per set. That
   gives f for (ii) and (iii) and settles whether the hot conflicts are pairs or longer chains.
2. **Build the L1 fix the count picks (S for (ii): a constant, the clear cost and the owner arrays; M for (iii)), and
   measure it alone on make** (gate: `miss` down by at least 70% of f x 87.5 M x 0.5 us; capacity flushes not up by
   more than one; `l1` and `guest` not up by more than half the gain). This removes the trap for the largest miss class
   with no new resident path and no banking question, which is why it goes first.
3. **Then the page record (its chunks 0-1) and the fast miss path's chunks 1 and 2 built together and measured as one
   change**: configure's gain needs both (section 3), and chunk 1 alone is neutral there. The make gate for the fast
   miss is recomputed on the conflict misses the L1 fix leaves ((1 - f) x 87.5 M) plus the revalidation misses chunk 2
   takes (34.8 M): at f = 0.7 that is about 26 M + 35 M taken misses, about 11-15 vCPU-s net, and the gate becomes 60%
   of that low end (about 6.5 vCPU-s) rather than the 8 of section 5.
4. If the count shows f under 0.3 (the conflicts are long chains, not pairs), skip the L1 fix and build the fast miss
   path first with the gates of section 5 as they stand.

Why not the fast miss path first: its make gain is largest exactly on the misses the L1 fix removes for free, so
building it first would make the L1 fix look worthless and leave the trap in place; and its configure value is zero
until chunk 2. Why not both L1 options: (iii) is the fallback if the count shows pairs dominate and the code area
cannot spare the memory for 18 bits; a count decides, not a guess.

## 8. Review response (independent review, November 14, 2026: accept with fixes; design sound, tests blocking)

| Finding | Resolution |
| --- | --- |
| Blocking: chunk 1 takes only misses into current blocks not in L1; `miss-counts` never took the path (the first miss fills L1 in `find`), `miss-step` and `miss-ac` ran into non-current targets, and the PIC `entry()`/`full_entry()` mutant was never reached (PIC programs miss only after CR3 changes) | Fixed (5): every fast-path test warms V in the present epoch, evicts its L1 entry with a block at V + 64 KiB in the same code context (DR7 and AC are part of the context), then jumps; `miss-counts` asserts `miss_fast` and the L1 hit on the second jump; new `miss-pic` builds a PIC conflict; mutations re-mapped |
| Blocking: the hook cases could not tell mutants apart (the build always declined; the host's kick at the `eret` masks a skipped check); the IF-from-record mutant bites only with record IF 0 and page IF 1 | Fixed (5, 2.1 check 6): the hooks build runs the real checks; the harness posts without ringing the doorbell and each case asserts the per-reason decline counter; case (c) enters with IF clear and executes an inline `sti` before the jump |
| Invariant's precedent and the pre-bank readers | Stated (2.2): the L1 hit and the patched link are the precedent; after a fast miss the record and page equal what an L1 hit from the last full entry leaves, plus RF cleared and `shadow_entry` 0; the reviewer's reader list recorded, the grep confirms it at build |
| Step-block exits now reach `miss_fast` | Stated (2.1 check 2): X2's contract is kept by the debug, boundary-state, event and uncached checks |
| "The same holds for `division`" and "a push would fault loudly" were false | Fixed (2.1, 6): this is the first path resuming from the gate frame on the exception stack; a push would silently overwrite the frame's `sp`/`nzcv`; a comment at `resume_exception` states the contract |
| PSTATE | `BLOCK_SPSR | frame.nzcv`, as `enter` (2.1), with a mutation row |
| `events::none` reads the record's IF | The page's IF bit is written into the record first at CPL 0; no API change (2.1 check 4) |
| `STI_FIRST` targets, feature builds, the decline counter, `lapic::due`'s backlog | Stated (2.1): entered as an L1 hit; `pic-count` and `dispatch-study` decline before the probe; plain increments; the backlog is a decline |
| Gain: declines cost about 45-50 ns each; make's 315 ns may include drain work | Costed (3): chunk 1 is neutral or a small loss on configure (gated on no regression), 13-19 vCPU-s net on make; configure gains only with chunk 2; the per-taken-miss saving is measured before the 0.18-0.25 us is trusted; the repeated-probe fix kept in reserve |
| Decision asked: fast miss vs a bigger or 2-way L1 for make's conflict misses, and the order | Section 7: count first (shadow owner tables at 17/18 bits and 2-way), build the L1 fix the count picks and measure it alone, then the page record with the fast miss path's chunks 1 + 2 as one change with a recomputed make gate |

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

**Checks, all reads** (the record, `Control`, the shared words), in this order, each a decline reason counted as
`sys_fast_declined` is (`declined`, `:765`; `SysFastDecline`, abi `execution.rs:2840`):

1. Debug: `s.debug != 0`, RFLAGS.TF, DR7 enables (`:697`'s test in `sys_fast`).
2. Boundary state: `s.cpu.mov_ss | s.sti_shadow | s.mid_string | s.plain_step | s.stop_wanted != 0`.
3. Pending work: `cache::pending()` (posted pages, a flush or shadows request), `io_pending(s)` (an I/O window),
   `lapic::due()` (an expiry or backlog).
4. Events: `events::none` with IF as the block left it: at CPL 0 the frame page's IF byte (`frame::IF`, what `bank`
   reads, `:2675-2681`), at CPL 3 the record's. A takeable event declines.
5. Uncached shadows: `Control::uncached_count | uncached_fence != 0` (`uncached_end` would unmap, `:384-399`).
6. Test hooks: `Control::pic_test != 0`; the `io-test-hooks` build declines after reporting its point (2.4).
7. The target: `context = s.cpu.code_context()` (generated code changes none of its inputs: CPL, the control registers,
   the MXCSR and FCW masks, XCR0, RFLAGS.AC, DR7, as `sys_fast`'s contract states, `:660-662`); `cache::select_l1(context)`;
   `cache::current_at(rip, context)` with `rip = frame.x[0]`. No block, a `SYSTEM` decode (the full path executes it) or a
   `STEP` block declines. Chunk 2 adds the page-record revalidation (2.3).

**Commit**, all writes resident-side: `s.cpu.rflags &= !RF` (`completed`'s effect; `stepped` is a no-op with `debug`
0); `s.shadow_entry = 0` (what `enter` writes with no shadow, `:1489-1493`); `s.blocks += 1`, `s.l2_hits += 1`;
`cache::fill_l1(rip, target)` (the L1 refill with its code synchronization, as today; item 1's IDC gating shortens it);
`tables::select(s.cpu.cpl)` (returns at once: the CPL is the exit's); `perf::enter(GUEST)`; then
`resume_exception(frame, pc, spsr, alignment_check(&s.cpu))` with `pc = cache::code_of(target).full_entry()` and
`spsr` the gate's SPSR_EL1.

**Why the exit frame is the entry frame.** An L1 hit enters `full_entry` with x0 = RIP and every other register as
the exit left it (`gates.S:239-253`; `cache::l1_words`). The gate's frame holds exactly that state: x0 is the RIP, the
static registers the guest GPRs, the PF/AF registers, the Q registers, FPCR/FPSR and the exit's NZCV in SPSR. For a
`PIC` target `full_entry` is the `and x0 / str x0, [x28, #PAGE_BASE]` prologue (`Block::full_entry`, abi
`execution.rs:817-823`), which writes the page base itself, so the fast path writes no frame-page field at all. The
frame lies below the exception stack's top; `resume_exception` (`gates.S:161-229`) resets SP to that top and then only
loads from the frame before `eret`, so nothing overwrites it (a review point: the same holds for `division`'s resume
from the copy, and `enter_allocated` is `resume_exception` with `spsel` set).

### 2.2 The invariant this rests on, and what stays stale

**The state record is authoritative only at a full entry, after `bank`; while generated code runs, the registers and
the frame page are.** This already holds today (the record's GPRs, flags and `rip` are stale during a block); the fast
path extends "while generated code runs" across a miss. Every full entry banks before it reads guest state (`missed`
`:1749`, `linker` `:563`, `sys_fast` `:708`, `interrupted`'s `at_once` `:1870`, `stopped`, `aborted` from the site) and
every one computes RIP from the frame or the faulting site, never from the record (`:564`, `:1751`, `:1871`, `:2796`).
After a fast miss the record's `cpu.rip`, GPRs, DF/IF, GS bases and XMM are those of the *previous* full entry, which
the next full entry's `bank` replaces from the live state. The implementer greps every reader of `s.cpu.*` reachable
without a preceding `bank` (today: `halt`'s diagnostic copy, `resident_fault`) and the review confirms the list.

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
- **Stops, steps, hooks.** TF, `debug`, DR7, `plain_step`, `stop_wanted`, `pic_test` and the `io-test-hooks` build
  decline (2.1). The host's stop RIP never has a block, so a miss there takes the full path. `--check-reuse` is
  unaffected (no placement). `link-check` is unaffected (no link made).
- **Speculation barriers.** The path issues no walk and loads nothing from guest memory; it leaves through `eret`.
  The walk's `sb` is the full path's; chunk 2 inherits the page plan's barrier under the switch.
- **Relink** (relink plan): runs inside `lookup` after `checked` is written, resident-side, halting if `armed != 0`;
  `disarm` ran in `returned`. Unchanged on this path.
- **Capacity.** No placement, so no flush can happen here; a flush request pending (`cache::pending`) declines.

## 3. Cost and gain (estimates; the per-miss cost is measured in chunk 1)

Fast path, in the `miss` bucket: `returned`'s prologue about 15 ns; the checks about 15-20 ns (a dozen loads, one
atomic); `select_l1` about 5 ns (90 ns on a context switch, on both paths); the L2 probe 27-35 ns (bucket `lookup`,
unchanged); `fill_l1` about 40-60 ns (a 32-byte write and `dc cvau`, `dsb`, `ic ivau`, `dsb`, `isb`, `cache.rs:1369-1387`;
less with item 1); the commit's stores about 5 ns. About **110-150 ns** against today's 320 / 430 ns: a saving of about
**0.13-0.18 us per taken miss on configure, 0.18-0.25 us on make** (make's higher miss cost is cache misses on records
and L2 lines, which the probe keeps). The gate's save and restore stay in `guest`.

Eligible misses (census shares; 95% assumed taken after declines, which `sys_fast` suggests: 0.002% boundary declines
on `hackbench`):
- Chunk 1 (current blocks): configure 17.7% x 32.0 M = 5.7 M; make 66.3% x 131.9 M = 87.5 M.
- Chunk 2 (plus page-record hits at the record's 89-91% hit rate): configure + 0.544 x 0.9 x 32.0 M = 15.7 M, 21.4 M
  in all (67%); make + 0.29 x 0.91 x 131.9 M = 34.8 M, 122 M in all (93%).

| | configure (109.5 busy vCPU-s; serial) | make -j4 (561.5; wall / 4.1) |
| --- | --- | --- |
| Chunk 1 | 5.7 M x 0.95 x 0.13-0.18 us = **0.7-1.0 s** (0.6-0.9%) | 87.5 M x 0.95 x 0.18-0.25 = **15-21 vCPU-s** (2.7-3.7%; about 3.6-5 s wall) |
| Chunk 2 adds | 15.7 M x 0.95 x 0.13-0.18 = 1.9-2.7 s; total **2.6-3.7 s** (2.4-3.4%) | 34.8 M x 0.95 x 0.18-0.25 = 6-8 vCPU-s; total **21-29** (3.7-5.2%; about 5-7 s wall) |

The review's figures were 6.4 s / 26 vCPU-s; configure is lower here because its 27.8% "needs translation" misses can
never be fast, and because the saving per miss is taken from the measured buckets rather than 0.2 us flat. The
revalidation walks chunk 2's misses also skip are the page plan's gain, counted there.

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
- Tier 1: every `run_k1.py` program at 1 and 4 vCPUs (K1 reaches blocks by `jmp r13`, a computed exit, so
  `cr3-switch`, `remap-cr3`, `remap-invlpg`, `cross`, `global`, `place-global`, `contexts`, `local-write`, the `reuse-*`
  and PIC programs all exercise the fast path and its currency rule); `link-check`; `run_k2.py` (the `sysfast` cases
  still run `sys_fast`); `run_slice3_vm.py events delivery smp coherence`; `run_x3.py` (timers); `run_s4.py` (debug);
  the X2 shadow and #AC tests; `--check-reuse` boot with 0 mismatches. New short programs (K1 stays at its length):
  - `miss-step` (QEMU): TF set by `popf`, then `jmp r13` to V: the single-step #DB is delivered after V's first
    instruction (the handler records RIP). Also DR0 as an instruction breakpoint at V with TF clear: #DB before it.
  - `miss-ac` (QEMU): CR0.AM and RFLAGS.AC at CPL 3, `jmp r13` to a block whose first instruction is a misaligned
    load: #AC (the resume must set SCTLR_EL1.A).
  - `miss-system` (natemu only): `jmp r13` to a RIP holding a kept CPUID decode, twice: the second round (the decode
    kept and current) must execute it and reach the next block (values and the `systems` count).
  - `miss-counts` (natemu only, the `counts` form): two blocks jumping to each other by `jmp r13` K times: misses per
    round must be 2 for the first round and 0 after (the refill works), and `miss_fast` must equal the misses taken.
  - Hook cases (`io-test-hooks` build, `run_io_window_cases.py`, a new point `MISS_FAST_CHECK` reported before the
    checks): (a) an I/O window published while the vCPU is at the point: the fast path declines and the window is
    acknowledged before any guest access (the existing window assertions); (b) the host writes the target's code page
    while the vCPU is at the point (a `FLUSH`/`PAGES` post): the new bytes run; (c) an interrupt posted at the point
    with IF set: delivered at the target's first instruction (the handler records RIP = V).
- Tier 2 gate: `miss` + the miss share of `enter` + `enter_copy` down by at least 60% of the estimate's low end on make
  (at least 9 vCPU-s), `lookup` and `link` unchanged within noise; the taken share at least 60% of current-block misses;
  configure's small gain is recorded, not gated. Wall time judged in group R (items 1-3), with the page plan if measured
  together.

**Chunk 2: page-record hits** (after the page plan's chunk 1): `Mapped::Unknown` in `lookup`, `record_only` on the fast
path. Gate: the taken share at least 60% of all misses on configure and `miss` down by at least 60% of chunk 2's low
end (1.9 s configure, 6 vCPU-s make); the page plan's K1 programs (all computed jumps) green; `page-counts` shows the
hits now taken by the fast path (`miss_fast` counts).

**Deliberate mutations, each turning its test red:**

| Mutation | Caught by |
| --- | --- |
| takes a block that is not current (`current_at` -> `kept_at`) | `cr3-switch`, `remap-cr3`, `remap-invlpg`, `page-share` (a) |
| enters at `entry()` instead of `full_entry()` | `reuse-alias` and the PIC K1 programs (an instance runs with the wrong page base) |
| ignores TF or `debug` | `miss-step`, `run_s4.py` |
| ignores DR7 | `miss-step` second part |
| skips `fill_l1` | `miss-counts` |
| skips `io_pending` | hook case (a) |
| skips `cache::pending` | hook case (b) |
| skips `events::none`, or reads IF from the record instead of the page at CPL 0 | hook case (c) with the interrupt posted after an inline `sti` |
| resumes without SCTLR_EL1.A | `miss-ac` |
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
- Risks: the stale-record invariant has readers the grep must find (`halt`'s diagnostics are known; a path that
  resumes mid-block and reads `s.cpu.rip` from the record would be a latent bug today as well); the decline rate on
  make may be higher than `sys_fast`'s if posted invalidations or windows often coincide with misses (counted);
  `fill_l1`'s synchronization is the largest remaining term and depends on item 1 landing first; the gate frame's
  reuse under `resume_exception` relies on the exception stack layout (reviewed, not tested by itself: a mutation that
  pushes after the SP reset would fault loudly).

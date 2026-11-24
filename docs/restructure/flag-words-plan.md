# Fewer flag words at unit exits (plan)

Status: plan, revision 2, November 23, 2026, on `m2-sampler`. Design only: nothing is built or run for it. It is the "flag
words" row of the [EL0 sampler findings](el0-sampler-findings.md) (section 7, line 196: build, `zstd` first), under the
owner's decisions there (lines 204-210): correctness first, pooled measurement at the end, a kill-test and its own tests
per fix. Revision 1 (5fef92a4) was reviewed: version 0 sound with two conditions, version 1 not buildable from it. This
revision takes the conditions in (section 4: B1, B2), defers version 1 (section 5), adds the sample weighting (section 8)
and makes the first cut smaller (restore words as ordinary body words, no change to the resident or admission). Order:
**this fix is built last of the six.**

## 1. Why

The sampler's role table (findings section 4, line 133, M2 Max, a7cd8e82) puts 13.8% of `zstd`'s compress-phase EL0
samples raw, 22.8% skid-corrected, on flag words; `build` 5.4 / 11.4, the make step 3.9 / 10.8, `sqlite` 3.6 / 11.3,
`luajit` 3.5 / 14.8. The truth lies between the two figures (findings lines 53-57). "Flag words" is
`tests/bench/sample_roles.py`'s `flags` role (lines 11-13, 168-182): `rmif`, `setf`, `cfinv`, `ccmp`, `cset`, NZCV moves
other than slice 1's `mrs x25, nzcv`, and writes of the PF or AF register by a word that sets no flags. `mrs x25` is
`nzcv_save`, natemu's group (lines 53, 60), not in the 13.8%. A flag-setting `adds`/`subs` is FEX's ALU work, and so is the
`mov xR, x26` that moves a result out of the PF register (`roles()`, lines 308-316): flag cost the row does not count.

Which flag words the hot samples sit on (an experiment: scratch scripts `flagkinds.py`, `cause.py` in this session's
scratchpad, over the kept `guest-{zstd,build,sqlite,luajit}-sample` reports of run 20261123-213409-4d92, M2 Max, a7cd8e82:
the flag-role words of each report's top-50 blocks, decoded from its `.objects` file):

| Case | flag samples on top 50 | AF writes | `cfinv` | `cset` | `ccmp`/`rmif` | PF writes, other | live only for a unit exit |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `zstd` | 3,503 | 42.8% | 38.6% | 15.5% | 3.2% | 0% | 68% (taken edge 41%, unit end 27%) |
| `build` | 450 | 41.1% | 13.1% | 12.9% | 30.7% | 2.2% | 29% (24%, 5%) |
| `sqlite` | 598 | 58.0% | 17.1% | 0% | 2.8% | 22.1% | 30% (13%, 17%) |
| `luajit` | 347 | 49.9% | 3.5% | 0% | 0.3% | 46.4% | 60% (52%, 8%) |

"Live only for a unit exit": going forward along the fall-through, the flag is rewritten before any fault point of a later
instruction, and a branch to a literal exit (taken edge) or the unit's end (unit end) came first. The rest is live at a
fault point (`sqlite`: 67% of its flag samples), or is not bookkeeping: `cset` is mostly SETcc's own result, and
`ccmp`/`rmif`/`cset` are also IMUL's and INC/DEC's CF and OF. The script reads Arm words, not FEX's IR.

The hottest `zstd` unit, 0x44c1e8 (`ZSTD_RowFindBestMatch_noDict_5_6`, 23.5% of its block samples; 15 instructions, main
path 30 words), is one iteration of the loop over a row's tag matches. `cmp edx, r10d; jb` costs `eor x27, x5, x14` (AF),
needed only by the `jb` exit: the fall-through's `sub edi, 1` rewrites AF. `and rax, rdx; je` costs `mov x4, x26` (PF) and a
`cfinv` (1,287 samples raw, the busiest flag word in the run), needed only by the `je` exit: the fall-through's `test`
rewrites every flag. Both taken edges leave the loop (section 8).

## 2. What exists (read in the code)

**FEX's flag elimination** (`third_party/FEX/FEXCore/Source/Interface/IR/Passes/RedundantFlagCalculationElimination.cpp`
at the pin 13a1bfcf, `tools/fexc/build.sh:14`): a backward liveness over the unit's IR blocks, iterated to a fixed point
(Run, 703-776), so an extended unit (`--ext-conds 4`, [layout page](translated-code-layout.md) section 3;
`Frontend.cpp:1186-1198`) is one scope already. A block ending in an ExitFunction starts with every flag live
(ProcessBlock, 529-548): no liveness crosses a unit exit. StorePF, StoreAF and CarryInvert are removable writes (292-296,
321-322); InvalidateFlags (AND's AF, MUL's PF and AF) is a killing write (406-450). FEX also keeps PF and AF raw (PF's
register holds the result, AF's the operands' XOR; read lazily, `OpcodeDispatcher/Flags.cpp:174-192, 222-245`), reduces PF to
one bit where a writer stores 0 or 1 (OptimizeParity, 638-701) and folds a compare into `cbz` when no flag outlives the
branch (FoldBranch, 480-525, 777-797). Nothing drops PF or AF, and nothing at the pinned upstream base crosses an exit.

**What slice 1 changes there** ([slice-1 contract](slice1-contract.md) lines 19-20, 49, 55-58): every fault point reads
all six flags at its instruction's start (Classify 347-350; 555-566; `FaultPoint`: a guest memory access, a divide or a
check, `tools/fexc/shim/fexc_precision.h:21-34, 52-62`); guest memory ops are never removed (454); an instruction whose
NZCV write precedes one of its own fault points saves its incoming NZCV in x25 (716-735, `fexc_precision.h:63-66`).

**CF's form.** FEX keeps CF inverted in PSTATE.C at IR block boundaries (`CFInvertedABI`, `OpcodeDispatcher.h:1384`):
`FlushRegisterCache` rectifies it (1194-1198, `RectifyCarryInvert` 1838-1855: a `cfinv`), and `StartNewBlock` assumes it
(151-158: `CFInverted = CFInvertedABI` at 155). `CondJUMPOp` (`OpcodeDispatcher.cpp:672-743`) flushes before its CondJump
(695), then emits the taken side, always a new exit block at the unit's end in slice 1 (696-697, 714-726), before the
fall-through (728-740); each decoded block of an extended unit begins with `SetNewBlockIfChanged` and `StartNewBlock`
(`Core.cpp:594-598`). So one `cfinv` before a branch serves both edges. IR values do not cross IR blocks
(`IRValidation.cpp:73`).

**Exactness at the resident.** Every exit and fault banks through `bank` (`crates/runtime/src/execution.rs:3421-3441`):
guest flags from NZCV and the PF and AF registers by `guest_flags` (`crates/abi/src/execution.rs:3248-3257`: PF from the
parity of the PF register's low byte, AF = `(af ^ pf) & 16`; entry by `native_flags`, 3240-3246); exits with CF inverted
(the linker 707-712, the dispatcher miss 2415-2420, interrupts at `at_once` 2576-2582), a fault with its site's
`cf_inverted` and NZCV from PSTATE or x25 (`bank_at`, 3558-3566). A doorbell inside a block arms a stop at every exit word
(`cache::arm`, `crates/runtime/src/cache.rs:2162-2205`); at a block's first word, or in the dispatcher or L1, the vCPU
stops at once (`interrupted`, 2532-2537, 2605-2637). The linker rewrites a literal exit's first word, the move of its
target into x0 (`cache::link`, 2292-2420; a split block's trampoline, `Block::link_at`, `crates/abi/src/execution.rs:1388-1397`).

## 3. Which flag words are avoidable exactly

1. **Live only on a taken edge to an exit, dead on the fall-through** (`zstd` 41%, `luajit` 52%, `build` 24% of the flag
   samples above): restore them on that edge, in the exit block before its move, and drop them from the body. Exact with
   no new run-time knowledge. **Version 0, the only version this plan builds.**
2. **Live only because of an exit whose target rewrites them first:** a link that skips the restore. Version 1,
   deferred (section 5).
3. **Live only at a fault point** (`sqlite` 67%): exact only if the resident recomputes PF and AF at the fault from a site
   recipe. Version 2, a lead (section 10).
4. **Not avoidable this way:** SETcc's `cset`; IMUL's and INC/DEC's CF/OF words, whose inputs are temporaries gone by the
   exit; `mrs x25`.

## 4. Version 0: flags restored on the exit edge

Placement: the FEX fork, where FEX's own logic lives (AGENTS.md, "The FEX fork"): the CF rule in `CondJUMPOp`,
`StartNewBlock` and the block loop (`Core.cpp:594-598`), the restore and its cleanup in the flag-elimination pass. Behind a
new request argument `--exit-flags`, sent by the host with the others (`TRANSLATION_ARGS`, `crates/host/src/execution.rs:2720`;
the host's full set as `tests/static/check_sample_roles.py:22` uses it). Off, fexc's output is byte-identical; the argument
is part of the request, so of the persistent cache's key.

**B1, CF: a single-predecessor rule.** Only `CondJUMPOp` (Jcc), and only when its taken side is a new exit block (always in
slice 1) and its fall-through is the next decoded block of the same unit or a new exit block. `CondJUMPRCXOp` (JRCXZ,
`OpcodeDispatcher.cpp:744`) and `LoopOp` (LOOP/LOOPE/LOOPNE, 796) keep today's rectify.
- Before the CondJump, save the dispatcher's `CFInverted` as S and flush without `RectifyCarryInvert`.
- The taken exit block, emitted first: after its `StartNewBlock` (which sets the ABI form), set `CFInverted = S`; its exit's
  flush then rectifies, so the `cfinv` is in the exit block when S is not the ABI form.
- The fall-through: restore `CFInverted = S` at its start, after `StartNewBlock` (the block loop's, `Core.cpp:598`, for the
  next decoded block; `CondJUMPOp`'s own, 736, for a new exit block).
- Assert, in the fork, that any IR block entered in a form other than the ABI form has exactly one predecessor, the
  CondJump's block (true in slice 1: every taken edge is an exit, 696-697, and an extended unit grows forward only); a
  second predecessor is a compile failure, never code.
- The PC table's and each site's CF form come from the dispatcher state at each instruction start
  (`tools/fexc/shim/fexc_hook.h:122`), so they follow the carried state.

**B2, PF and AF recipes.** The exit block restores the unit's last PF or AF write before the CondJump only by one of these;
anything else stays in the body as today.
- PF: only when StorePF's argument is, by SSA identity, the very node the instruction stores to a guest register R, and R
  is not written again before the CondJump: `mov x26, xR`. Any other PF source (a variable-count shift's conditional PF, a
  select, a partial merge) is not matched. PF of `cmp`/`test` (no result register) is not restored: its `subs x26`/`ands x26`
  costs no word in the body.
- AF: guest AF is `(af ^ pf) & 16` (`guest_flags`, above; `LoadAF`, `Flags.cpp:182-191`), so an AF recipe is exact only if
  at the exit the PF register holds the same instruction's result: either PF stays in the body from that instruction, or
  the PF recipe above restores it from the same instruction. Forms:
  - `cmp`/`test` of two guest registers, neither written before the CondJump: `eor x27, xA, xB` (1 word).
  - `cmp R, k`: `mov x27, xR` or `mvn w27, wR` (FEX's own rule for a constant with bit 4 set, `Flags.cpp:227-241`; 1 word).
  - `add`/`sub R, k`, `inc`/`dec R` (k = 1): R's old value is R minus (plus) k: `sub`/`add x27, xR, #k`, then `mvn` for a k
    with bit 4 set (1-2 words).
  - `add`/`sub R, S` with S a guest register not written before the CondJump and S not R: the writer clobbers R, so
    `sub`/`add x27, xR, xS; eor x27, x27, xS` (2 words).
  Operands are whole guest registers or their low parts (no AH-style operand). A recipe is emitted only where the pass
  then removes the body's copy; after the pass, a restore whose body copy survived (another path reads it) is removed
  again, so no path gains a word.
- Only exits a Jcc's taken side creates. The unit's final exit, computed exits, REP self-exits, the retry exit of a locked
  NEG (`tools/fexc/shim/fexc_sites.inc:1755-1779`) and step units (`--max-insts 1`) are left as they are.

**First cut: the restore words are ordinary body words.** Checked in the code: `body_end`
(`crates/abi/src/execution.rs:2165-2207`) walks the words reached from the entry by falling through and by forward branches,
and stops only at exit words (each link point's first word, the move: `exit_offsets`, 2210-2223) and stub first words. A
Jcc into an exit block now lands on its first restore word, which is reached and not a stop, so `body_end` takes the
restore words into the hot copy and the exits before them stay in place: `Block::trampolines` puts an exit at or before
the body's end at its own offset in the hot copy (1377-1386), `split` writes there `trampoline_word`'s `b` to the cold exit
(`crates/runtime/src/cache.rs:117-160`; `trampoline_word`, `crates/abi/src/execution.rs:2227-2235`), and that word is what
`link_at` returns for a link and an arm: after the restore words. The copied `movz`/`movk`/`svc` words of exits in place are
dead in the hot copy (`moved_word`, 2107-2126, copies them as they are; it refuses only `adr`, `adrp` and literal loads,
which exit blocks do not have). The taken path is the same two branches as today (the Jcc, then the in-place word's `b`).
Admission needs nothing new: restore words are the PF/AF register writes and `cfinv` FEX emits in bodies today, and a
branch into an exit block enters no link point after its first word. Nothing in the resident changes, so a block without
restore words is placed, split and linked byte for byte as today; with them its hot copy also carries the dead exit words
(about 3 per exit in place), a cost to the hot zone the kill-test counts. A later cut could move the restore words into
trampolines instead; not planned.

**Extra `mrs x25` saves.** Carrying CF unrectified can move a `cfinv` into a later instruction (one that reads CF, or the
next CondJump's own flush) before one of its fault points; that instruction then saves its incoming NZCV (716-735). The
static case counts `nzcv_save` words with and without the argument; any increase on the measured functions is reported, and
an increase on a hot unit's main path fails the kill-test like any other word.

**pic-count.** Version 0 makes full links only. The `pic-count` study's counting stubs do not build at all
(`compile_error!`, `crates/runtime/src/cache.rs:72`), so nothing there changes.

## 5. Version 1 (deferred)

A link that skips the restore words when the target's own analysis says it rewrites those flags before any read, fault
point, exit or resident entry. Not built from this plan; decided at the pooled profile round. Why deferred:
- Undefined flags would become timing-dependent: a target whose first flag write is AND (AF undefined) or MUL (PF, AF)
  kills them, so their value after it would depend on whether the exit before it happened to be linked, that is, on the
  run's history. If it is ever built, use the strict rule: only a defined write kills.
- The target's entry need should not be fexc's word: admission should derive it by scanning the target's straight-line
  prefix (flag reads, flag writes, fault points, exits, resident entries) and default to "all live".
- It changes linking, arming (the skipped restore must run before an armed stop) and the first-word interrupt rule; each is
  an observation point to prove, as the review found. The static case reports, for each exit with restore words, whether its
  target's prefix would allow a skip, so the round has the number.

## 6. The exactness argument, by observation point (version 0)

| Point | Reads | Version 0 |
| --- | --- | --- |
| Guest fault at a site (`bank_at`) | NZCV or x25, PF, AF, the site's CF form | body flags at fault points unchanged (the slice-1 rule stays); CF form per instruction from the carried state (B1) |
| Unlinked literal exit (`linker`) | all six, CF inverted | the restore words ran before the move; the exit block rectified CF |
| Linked exit | target's entry state | the link patches the move (or its in-place word), after the restore words |
| Computed exit, dispatcher, L1, miss | all | not touched |
| Armed stop at an exit (`stopped`) | all | `link_at` is after the restore words |
| Doorbell in a block, in restore words | nothing (arms) | the restore words finish, the stop is at the exit |
| Doorbell at a block's first word | entry state | entries exact as today |
| Single step (TF), step blocks | all | step units not touched, never linked |
| Instruction breakpoint | entry state | blocks at breakpoints never linked (`execution.rs:514`) |
| Data breakpoint, watch | at the store | stores are fault points |
| PUSHF, LAHF, SETcc, ADC, Jcc in code | the flags they name | FEX's pass, unchanged |
| System instruction (a cut) | all | its exit is never linked |
| Helpers (`svc` divide, x87, string) | all | divide is a fault point; not touched |
| The resident's reconstruction | NZCV, PF, AF | formulas unchanged |
| Host inspection, watchdog | approximate between sites today | same |

## 7. Smallest useful version

Version 0 as above is the whole build: compile-time only, every exit exact, the resident and admission unchanged.

## 8. Kill-test

**Weighting (an experiment, done for this revision):** `flow.py` and `cause3.py` (scratchpad) over the same kept
`guest-zstd-sample` report and objects (run 20261123-213409-4d92, M2 Max, a7cd8e82). The words version 0 removes from bodies
carry 1,441 of the 3,503 flag samples: unit 0x44c1e8's `cfinv` before `je` (1,287) and AF `eor` before `jb` (96); in
`ZSTD_compressBlock_lazy2_row` 38; unit 0x44c2c0's memory compare before its back edge (14) and unit 0x44c219's `cfinv`
before `b.ne` (5). Whether the taken edges are hot:
- 0x44c1e8's three taken edges all go to 0x44c240, the code after the match loop; the fall-through goes to 0x44c219, which
  returns to 0x44c1e8 for the next match. 0x44c1e8 has 7,195 samples on a 30-word main path, 0x44c240 389 on 47 words, so
  0x44c1e8 runs about 29 times per entry of 0x44c240 at equal cost per word, and still about 10 times if a word of the
  loop costs three times one of 0x44c240. The `je` and `jb` exits together are taken on about 3-10% of iterations.
- The hot taken edges are 0x44c219's `b.ne` (the loop's next iteration) and 0x44c2c0's back edge: version 0 only relocates
  their words (19 samples), with no gain and no loss.
Weighted, version 0 removes about 1,300 samples' words from executed paths, 4% of the run's 30,595 block samples and about
5 points of the compress phases' flag share raw, nearly all one `cfinv`. **Version 0 is worth building**, with the caveat
that a removed word's samples partly move to its neighbour (the `cfinv` sits before a data-dependent `je`), so the time
saved is below that.

**Static, no VM (after the fork change):** a new bench case `flag-words-zstd` (`tests/bench/family.toml`, judged, no VM):
takes `zstd` from `{bench_image}`, compiles every unit of `ZSTD_RowFindBestMatch_noDict_5_6.constprop.0`,
`ZSTD_compressBlock_lazy2_row` and `ZSTD_row_update` (79.5% of `zstd`'s EL0 samples together, findings line 133) reachable
from their entries by literal exits, with the host's arguments with and without `--exit-flags`, and reports per unit:
main-path words, flag-role and `nzcv_save` words on it, restore words per exit, hot-copy bytes, and (for version 1's
decision) exits whose target's prefix would allow a skip. Expected (counted by hand from the kept objects, an experiment):
unit 0x44c1e8's main path 30 to 27 words. Kill if the summed main path over the three functions falls by less than 2%, or
any unit's main path grows (extra `nzcv_save` included).

**One VM case (after it):** `guest-zstd-sample` on the M2 Max, once: the compress phases' FEX flag share must fall by at
least 2 points raw from 13.8 / 13.9 (run1 / run2). Noise: the two compress phases of the kept run differ by 0.1 point raw
and 0.3 corrected, and the binomial spread of a 13.8% share over 12,500 samples is 0.3 points; 2 points is well above both.
Also unit 0x44c1e8's samples per compress phase must not rise. Otherwise the change is taken out before the pooled round.
Timing is the pooled A/B's.

## 9. Tests

Through `tools/natemu-test` only; each new check shown red on a mutation (applied, built, run, reverted, as
`tests/system/run_fastmiss_mutations.py` does).
- **Existing, to pass:** `programs` (`fix`, `seed`, `flow` on the control-flow corpus `tools/reference/gen_slice2.py`, at
  `engine.max_block` 0 and 1, QEMU answers with masked RFLAGS); `faults` (`faults-*`; `loopfaults-*`: faults inside loops,
  masked RFLAGS against QEMU's at the fault, `tests/faults/run_slice2_faults.py`); `system` `s4`, `x3-*`, `k1-*`, `pic-*`;
  `unit`; `static` (the `fexc` suite, `fexc-asm32`, `fexc-precision-32`); `correctness`; the gate before the merge.
- **`sample-roles`** changes by design (role sequences of idioms with a Jcc): revised with the corpus.
- **`fexc-replay`:** with `--exit-flags` off, every reply byte-identical (the case as it is, recorded with the argument
  off). With it on, a new comparator in `tools/fexc/replay.py` (like `only_moves`, 259-340): a reply may differ only by
  body words the classifier calls `flags` or PF moves removed, restore words added in exit blocks, `nzcv_save` words, and
  the offsets those shift; its own mutation check (one other change per reply must be reported).
- **New `exit-flags-shapes`** (static, beside `sample-roles`): an idiom corpus (`cmp`/`test`/`and`/`add`/`sub`/`inc`/`dec`
  with Jcc; operands clobbered or not; R = S; 8, 16, 32, 64-bit; AH-style operands; variable-count shifts before a Jcc; JRCXZ
  and LOOP; 32-bit code) with the restore words and body words written by hand, and a semantic check: each unit compiled with
  and without the argument runs natively from entry to every exit (`tools/fexc/exec_words.c`, extended to end at an exit's
  `svc` and report NZCV) on random registers, and the six flags by `guest_flags` must be equal at every exit and at every
  site's instruction start. Mutations: a PF recipe matched by value instead of SSA identity (the shift case goes red); an AF
  recipe when PF came from another instruction; the one-word form for `add R, S`; the exit block's CF form not set to S.
- **New split tests** (abi unit tests): a block with restore words splits with its exits in place and `link_at` after the
  restore words; a block without them gives today's `split_point`, `trampolines` and words, byte for byte.
- **New `gen_slice2.py` templates** ("flag-exit"): a taken edge to a label whose first instruction reads PF, AF or CF (SETcc,
  ADC, LAHF, PUSHF, `jp`, `ja`) while the fall-through rewrites them, and CF-reading instructions with a memory operand on
  the fall-through after a non-rectified Jcc, crossed with the `loopfaults` page mutations. Mutations: the fork drops the
  restore words but keeps the body deletion; the fall-through not given S back.

## 10. Version 2 (a lead, not planned)

Fault-site recipes: a site record names where PF and AF can be recomputed; fexc drops the words when only fault points
read them; `bank_at` recomputes. `sqlite`'s flag words are mostly of this kind. About as large as versions 0 and 1 together.

## 11. Estimate (agent days, low confidence)

| Part | Days |
| --- | ---: |
| fork: CF rule with the assert, recipes, cleanup, `--exit-flags` | 1.5 |
| `exit-flags-shapes` with `exec_words`, split unit tests, `gen_slice2` templates, replay comparator | 2 |
| kill-test case `flag-words-zstd` | 0.5 |
| total | 4 |

Plus VM time: the families above, the gate once, one `guest-zstd-sample` run. Expected gain, from the weighting (an upper
bound): `zstd` compress 1-4% of EL0 time, `build` and the make step under 1%, `sqlite` about nothing.

## 12. Risks

- CF carried across a non-rectified Jcc: a wrong CF form at a fall-through site gives a wrong CF at a fault (`loopfaults`,
  the new templates and the semantic check at site starts cover it); the single-predecessor assert keeps it local.
- A recipe reading a register written after the writer, or PF and AF from different instructions: the semantic check at
  every exit.
- The hot copy grows by the dead exit words of exits in place: the static case reports hot-copy bytes.
- A removed word's samples move to the next word: the VM case checks the hot unit's samples as well as the flag share.

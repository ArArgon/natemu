# Fewer flag words at unit exits (plan)

Status: plan, November 23, 2026, on `m2-sampler` at d3f2fe4d. Design only: nothing is built or run for it. It is the "flag
words" row of the [EL0 sampler findings](el0-sampler-findings.md) (section 7, line 196: build, `zstd` first) under the
owner's decisions there (lines 204-210): correctness first, pooled measurement at the end, a kill-test and its own tests
per fix. Not reviewed yet.

## 1. Why

The sampler's role table (findings section 4, line 133, M2 Max, a7cd8e82) puts 13.8% of `zstd`'s compress-phase EL0
samples raw, 22.8% skid-corrected, on flag words; `build` 5.4 / 11.4, the make step 3.9 / 10.8, `sqlite` 3.6 / 11.3,
`luajit` 3.5 / 14.8. The truth lies between the two figures (findings line 53-57: the correction moves every sample, also
those of cheap words). "Flag words" is `tests/bench/sample_roles.py`'s `flags` role (lines 11-13, 168-182): `rmif`,
`setf`, `cfinv`, `ccmp`, `cset`, NZCV moves other than slice 1's `mrs x25, nzcv`, and writes of the PF or AF register by a
word that sets no flags. `mrs x25, nzcv` is `nzcv_save`, natemu's group (lines 53, 60), and is not in the 13.8%. A flag-setting
`adds`/`subs` is FEX's ALU work, and so is the `mov xR, x26` that moves a result out of the PF register (`roles()`, lines
308-316): that move is flag cost the row does not count.

Which flag words the hot samples sit on (an experiment: `flagkinds.py` and `cause.py`, scratch scripts in this session's
scratchpad, over the kept `guest-{zstd,build,sqlite,luajit}-sample` reports of run 20261123-213409-4d92, M2 Max, a7cd8e82:
the flag-role words of each report's top-50 blocks, from its "Top blocks' words" section, decoded from its `.objects`
file):

| Case | flag samples on top 50 | AF writes | `cfinv` | `cset` | `ccmp`/`rmif` | PF writes, other | live only for a unit exit (see below) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `zstd` | 3,503 | 42.8% | 38.6% | 15.5% | 3.2% | 0% | 68% (taken edge 41%, unit end 27%) |
| `build` | 450 | 41.1% | 13.1% | 12.9% | 30.7% | 2.2% | 29% (24%, 5%) |
| `sqlite` | 598 | 58.0% | 17.1% | 0% | 2.8% | 22.1% | 30% (13%, 17%) |
| `luajit` | 347 | 49.9% | 3.5% | 0% | 0.3% | 46.4% | 60% (52%, 8%) |

"Live only for a unit exit": going forward from the word along the fall-through, the flag is rewritten before any fault
point of a later instruction, and a branch to a literal exit (taken edge) or the unit's end (unit end) came first. The
rest is live at a fault point (sqlite: 67% of its flag samples), or is not bookkeeping: `cset` is mostly SETcc's own
result, `ccmp`/`rmif`/`cset` also IMUL's and INC/DEC's CF and OF. The script reads Arm words, not FEX's IR, and knows
neither edge frequencies nor what the exits' targets need.

The two hottest `zstd` units (`ZSTD_RowFindBestMatch_noDict_5_6`, 23.5% and 15.2% of its block samples) show the pattern.
Unit 0x44c1e8 (15 instructions, its main path 30 words): `cmp edx, r10d; jb` costs `eor x27, x5, x14` for AF, which only the
`jb` exit needs (the fall-through's `sub edi, 1` rewrites AF); `and rax, rdx; je` costs `mov x4, x26` (PF) and a `cfinv`
(1,287 samples raw, the unit's busiest flag word), which only the `je` exit needs (the fall-through's `test` rewrites all
flags); `test r14b, r14b; je` costs `mov x26, x19` and a `cfinv` for its two exits, whose targets (0x44c240 and 0x44c219)
rewrite every flag before any memory access. Unit 0x44c2c0 (11 instructions, a loop whose `jne` exits to the unit's own
start, 18 words per iteration) computes the memory compare's AF for the back edge only, and the unit's first instruction,
`add rdx, 1`, rewrites every flag.

## 2. What exists (read in the code)

**FEX's flag elimination** (`third_party/FEX/FEXCore/Source/Interface/IR/Passes/RedundantFlagCalculationElimination.cpp`
at the pin 13a1bfcf, `tools/fexc/build.sh:14`): a backward liveness over the unit's IR blocks, iterated to a fixed point
across them (Run, 703-776), so an extended unit (`--ext-conds 4`, [layout page](translated-code-layout.md) section 3;
`Frontend.cpp:1186-1198`) is already one scope: a flag written before a conditional branch and rewritten on the
fall-through is dead there. A block that ends in an ExitFunction starts with every flag live (ProcessBlock, 529-548): no
liveness crosses a unit exit. StorePF, StoreAF and CarryInvert are removable writes (292-296, 321-322); InvalidateFlags (an
undefined result: AND's AF, MUL's PF and AF) is a write that kills (406-450). FEX also gives: PF and AF kept raw (PF's
register holds the result, AF's the operands' XOR, both read lazily: `OpcodeDispatcher/Flags.cpp:174-192, 222-245`), PF
reduced to one bit where a writer stores 0 or 1 (OptimizeParity, 638-701), and a compare folded into `cbz` when no flag
outlives the branch (FoldBranch, 480-525, 777-797). There is no option that drops PF or AF; the pinned upstream base
(48d71752) has nothing that crosses an exit either: FEX links its blocks at run time and does not know their successors.

**What slice 1 changes there** ([slice-1 contract](slice1-contract.md) lines 19-20, 49, 55-58): every fault point reads all
six flags at its instruction's start (Classify 347-350; the instruction-start rule 555-566; `FaultPoint` is a guest memory
access, a divide or a check, `tools/fexc/shim/fexc_precision.h:21-34, 52-62`), guest memory ops are never removed (454), and
an instruction whose NZCV write precedes one of its own fault points saves its incoming NZCV in x25 (716-735,
`fexc_precision.h:63-66`). Upstream FEX treats loads and stores as flag-transparent.

**CF's form at exits.** FEX keeps CF inverted in PSTATE.C as its block ABI (`OpcodeDispatcher.h:1384`); every IR block end
rectifies it (`FlushRegisterCache`, 1194-1198, `RectifyCarryInvert` 1838-1855, a `cfinv` with FlagM) and every new IR block
starts from that form (`StartNewBlock`, 151-158). A conditional branch flushes before its CondJump (`CondJUMPOp`,
`OpcodeDispatcher.cpp:672-743`, flush at 695); its taken side is always a new IR block at the unit's end that holds only the
exit (717-725). So a `cfinv` before a branch serves both edges, though only the exit edge needs the ABI form. IR values do
not cross IR blocks (`IRValidation.cpp:73`).

**Exactness at the resident.** Every exit and fault banks through `bank` (`crates/runtime/src/execution.rs:3421-3441`):
guest flags from NZCV, the PF register and the AF register by `guest_flags` (`crates/abi/src/execution.rs:3248-3257`; entry
by `native_flags`, 3240-3246); exits with CF inverted (the linker 707-712, the dispatcher miss 2415-2420, interrupts at
`at_once` 2576-2582), a fault with its site's `cf_inverted` and NZCV from PSTATE or x25 (`bank_at`, 3558-3566). A doorbell
inside a block arms a stop at every exit word (`cache::arm`, `crates/runtime/src/cache.rs:2162-2205`; `stopped`,
`execution.rs:2671-2676`); at a block's first word, or in the dispatcher or L1, the vCPU stops at once
(`interrupted`, 2532-2537, 2605-2637). The linker rewrites a literal exit's first word, the move of its target into x0, to
`b entry` (`cache::link`, 2292-2420; a split block's trampoline instead, `Block::link_at`, `crates/abi/src/execution.rs:1388-1397`).
A block at an armed instruction breakpoint is never linked to (`execution.rs:514`); step blocks are never linked (`linker`).
So today every observation point sees all six flags exact, and between them (inside a unit) they are not: FEX has already
dropped dead writes there.

## 3. Which flag words are avoidable exactly

1. **Words live only on a taken edge to an exit, dead on the fall-through** (zstd 41%, luajit 52%, build 24% of the flag
   samples above). Move them onto that edge: the exit block recomputes them from guest registers just before its exit, and
   the body loses them. Exact everywhere with no new run-time knowledge. This is version 0.
2. **Words live only because of an exit whose target rewrites them first.** At link time the target is known. If the
   target unit's own analysis says a flag is rewritten before anything reads it (no read, no fault point, no exit, no
   resident entry first), the link may skip the words that restore it. This is the "guarded prediction" of the M2 design's
   optimisation leads (`m2-32bit-design.md:346-372`), with the guard at the link instead of at entry: nothing is predicted
   at compile time; the linker checks the target's recorded need. Version 1.
3. **Words live only at a fault point** (sqlite 67%, build about 16%, zstd 13%): exact only if the resident can recompute
   PF and AF at the fault from a recipe in the site record (the result still in a guest register, the operands too).
   Faults are rare, so the cost would move to the fault path. Not planned here: version 2, a lead (section 10).
4. **Not avoidable this way:** SETcc's `cset`, and IMUL's and INC/DEC's CF/OF materialisation (`ccmp`, `rmif`, `cset`) whose
   inputs are temporaries gone by the exit. `mrs x25` is exactness for RMW faults and is not in this row.

Rejected: keeping the dropped flags only as a recipe in the exit's record, read by the resident when it banks there, with no
words in the stub. When the guard fails the exit could then never be linked, and every pass would trap.

## 4. Version 0: flags restored on the exit edge

**fexc.** Placement: in the FEX fork where FEX's own logic lives (owner ruling, AGENTS.md at 4743c184: the fork is
natemu's, IR pass changes are designed where they belong, under the same review and test rules): the CF change in
`CondJUMPOp` and `StartNewBlock`, the recomputation and its cleanup in the flag-elimination pass beside ProcessBlock, the
unit's entry need (version 1) read from that pass's CFG; the shim keeps the slice-1 policy hooks as today.
- CF: at a CondJump whose taken side is a literal exit, the flush before it does not rectify CF; the dispatcher state
  (`CFInverted`) carries into both successor blocks instead of `StartNewBlock`'s reset. The exit block's own flush then
  emits the `cfinv` when needed; the fall-through goes on in the form it has and rectifies only where it next needs to (a
  read of CF, its next CondJump, its end). The PC table's and each site's CF inversion come from the dispatcher state at
  each instruction start (`fexc_hook.h:122`), so they stay right.
- PF and AF: when the unit's last PF or AF write before a CondJump can be recomputed at the exit from guest registers and
  constants, the exit block gets that recomputation (StorePF, StoreAF before its ExitFunction). Recipes, the only ones
  accepted (anything else stays in the body as today):
  - PF of a result written to guest register R: `mov x26, xR` (1 word).
  - PF of `cmp`/`test`, AF of `cmp`, `test`, `add`/`sub` with both operands guest registers: one `sub`/`and`/`eor` of those
    registers (1 word).
  - AF of `add`/`sub`/`inc`/`dec`/`cmp` with a constant k and the result in R: R's old value is R minus (or plus) k (1-2
    words, with FEX's inversion for a k with bit 4 set, `Flags.cpp:227-241`).
  Each operand register must be unwritten between the writer and the branch, read as whole registers or their low part
  (no AH-style operand). The CF form and the recipe are emitted only where the pass then deletes the body's copy: after the
  pass, a recomputation whose body copy survived (another path reads it) is removed again, so no path gains a word.
- Behind a new request argument, `--exit-flags`, which the host sends with the others (`TRANSLATION_ARGS`,
  `crates/host/src/execution.rs:2720`; the host's full set as `tests/static/check_sample_roles.py:22` uses it). Off, fexc's
  output is byte-identical; it is part of the request, so of the persistent cache's key.
- Only exits that a CondJump's taken side creates. The unit's final exit, computed exits, self-exits of REP strings, the
  retry exit inside a locked NEG (`fexc_sites.inc:1755-1779`) and the step unit's exits are left as they are.

**The resident and admission.** The restoring words ("pre-words") sit before the exit's move, which stays the link point.
- Admission records each literal exit's pre-word count (the move's relocation has a spare byte, `code_object.rs:339`) and
  checks the pre-words: straight-line, no memory access, writing only the PF register, the AF register and NZCV (`cfinv`
  alone), and that every branch into an exit lands on its first pre-word. It derives the mask of flags they restore from
  the words (P for a write of the PF register, A for the AF register, C for `cfinv`), not from fexc.
- Hot/cold: an exit's trampoline carries its pre-words, then the word a link patches, so a branch out of the hot copy into
  an exit lands on the trampoline's first word and the link stays one taken branch. That changes the trampoline layout
  (`Block::trampolines`, `hot_body_for`, `link_at`, `crates/abi/src/execution.rs:1360-1397`; `split_point` and `split`,
  `crates/runtime/src/cache.rs:77-160`; `body_end` stops at an exit's first pre-word) and `native_offset`'s mapping of a
  trampoline word to its exit. `link_at` stays the single answer to "which word does a link patch and a stop arm": the
  word after the pre-words.
- `cache::ARMED` (`crates/abi/src/execution.rs:1158`) is unchanged in version 0.

**Exactness.** Every path out of the unit through a literal exit runs the pre-words before the word a link, a stop or the
`svc` acts on, so every observation point sees what it sees today. The body's flags at fault points are untouched (the
slice-1 rule stays). Section 6 has the full list.

## 5. Version 1: skipping the restore at a link

- fexc records the unit's entry need: the flags live at its first IR block's start after the pass (FEX's own
  `CFG.Get(entry)->Flags`), with every resident entry counted as reading all flags (fault points and exits already are;
  also every `svc` to a helper and every stub). One byte in the header (`Header::reserved`, `code_object.rs:270`), a new
  object version. Version 1 also emits pre-words at the unit's final exit and at self-exits (a loop's back edge exits to the
  unit's own start, `OpcodeDispatcher.cpp:696`), where only a link can save them.
- The linker makes a *skip* link (`b entry` at the exit's first pre-word, or its trampoline's first word) when the target is
  a code block entered directly (not a checked link through the dispatcher slot, not a step or system block, not at an armed
  breakpoint) and its entry need shares no flag with the exit's restore mask; otherwise the *full* link of version 0.
  Decided again at every link (a new target block is a new link); unlink restores the word the record kept.
- `cache::arm`: a skip-linked exit gets its first pre-word restored as admitted and the stop word after the pre-words, so
  the stop happens with the flags restored; disarm puts both back. `ARMED` grows to 4 words per link point, plus 2.
- A block entered by a skip link (a bit the linker sets on the target) takes a doorbell at its first word as it does
  inside a block: armed, stopped at an exit (as REP-string and shadow entries are today, `execution.rs:2605-2632`).
- Undefined flags. A target whose first flag write is AND (AF undefined) or MUL (PF and AF) kills them, as FEX does inside a
  unit today, so an undefined flag's value after such an instruction may depend on whether the exit before was linked.
  It already depends on unit boundaries (`--ext-conds`, `engine.max_block`), the tests mask undefined flags
  (`tools/reference/flags.py`), and the x86 value is any value. Owner decision if a fixed policy is wanted; the strict rule
  (only a defined write kills) keeps AF at most of `zstd`'s exits.

## 6. The exactness argument, by observation point

| Point | Reads | Version 0 | Version 1 |
| --- | --- | --- | --- |
| Guest fault at a site (`bank_at`) | NZCV or x25, PF, AF, site's CF form | body flags unchanged at fault points; CF form per instruction from the carried dispatcher state | a target with a fault point before its kill needs all flags: no skip |
| Unlinked literal exit (`linker`) | all, CF inverted | pre-words ran, the exit block rectified CF | same |
| Full link | target's entry state | pre-words ran | same |
| Skip link | nothing until the target rewrites the dropped flags | none | the guard; before the kill the target has no fault point, exit, helper or stub (each counts as a read) |
| Computed exit, dispatcher, L1, miss | all | no pre-words, all live (unchanged) | same; a checked link goes through the slot: full only |
| Armed stop at an exit (`stopped`) | all | stops at `link_at`, after the pre-words | a skip link's first pre-word restored while armed |
| Doorbell at a block's first word | entry state | entries exact | skip-entered blocks arm instead |
| Doorbell in a block, in pre-words | none (arms) | arm, pre-words finish | same |
| Single step (TF) | all after one instruction | step blocks never linked: `svc` path | same |
| Instruction breakpoint | entry state | blocks at breakpoints never linked | same |
| Data breakpoint, watch | at the store | stores are fault points | same |
| PUSHF, LAHF, SETcc, ADC, Jcc inside code | the flags they name | FEX's pass, unchanged | counted in the target's need |
| System instruction (cut) | all | its exit is never linked | same |
| Helpers (`svc` divide, x87, string) | all | divide is a fault point | counted as reads in the need |
| REP self-exit | all | not touched | its block's need is all (memory first) |
| The resident's reconstruction | NZCV, PF, AF | formulas unchanged | same |
| Host inspection, watchdog | approximate between sites today | same | same |
| EL0 sampler | nothing banked | same | same |

A damaged or forged entry need can only give the guest wrong flags, inside its own containment, as any forged object can
(`crates/host/src/execution.rs:2731-2733`); admission checks the pre-words themselves.

## 7. Smallest useful version first

Version 0 alone: compile-time, every exit exact, no change to linking, arming or interrupts; the resident changes only
trampolines and admission. It takes the hottest single word in `zstd` (the `cfinv` before `je`) and the 41% "taken edge"
share. Version 1 follows only if version 0's kill-test passes and the static count says the hot exits' targets allow
skips.

## 8. Kill-test

- **Static, no VM (after fexc's part of each version):** a new bench case `flag-words-zstd` (`tests/bench/family.toml`,
  judged, no VM): takes `zstd` from `{bench_image}`, compiles every unit of `ZSTD_RowFindBestMatch_noDict_5_6.constprop.0`,
  `ZSTD_compressBlock_lazy2_row` and `ZSTD_row_update` (with the first, 79.5% of `zstd`'s EL0 samples, findings line 133)
  reachable from their entries by literal exits, with the host's arguments with and without `--exit-flags`, and reports
  per unit: main-path words, flag-role words (`sample_roles`), pre-words per exit, and (version 1) the exits whose
  target's need excludes their restore mask. Expected from the objects of run 20261123-213409-4d92 (an experiment, counted
  by hand): unit 0x44c1e8's main path 30 words, 27 in version 0 (AF `eor`, PF move, `cfinv`), 25 in version 1 (`test`'s PF
  move and `cfinv`); unit 0x44c2c0's loop 18 words per iteration, 17 in version 1 only. Kill version 0 if the summed main
  path over the three functions falls by less than 2%, or any unit's main path grows; do not build version 1 if fewer than
  half of the hot exits that carry pre-words are skip-eligible.
- **One VM case (after version 1):** `guest-zstd-sample` on the M2 Max: the compress phase's FEX flag share must fall by at
  least 3 points raw (from 13.8), and new resident counters (links made skip and full, in the perf log) must show the two
  hot units' exits skip-linked. Otherwise the change is taken out before the pooled round.

## 9. Tests

Through `tools/natemu-test` only; each new check shown red on a mutation (applied, built, run, reverted, as
`tests/system/run_fastmiss_mutations.py` does).
- **Existing families to pass:** `programs` (`fix`, `seed`, `flow` on the control-flow corpus `tools/reference/gen_slice2.py`,
  each at `engine.max_block` 0 and 1, QEMU answers with masked RFLAGS); `faults` (`faults-*`, and `loopfaults-*`: faults
  inside loops with QEMU's masked RFLAGS at the fault, `tests/faults/run_slice2_faults.py`); `system` `s4` (TF, DR7),
  `x3-*` (interrupts), `k1-*` (links, `k1-full-links`), `pic-*` (checked and local links); `unit`; `static` (the `fexc`
  suite, `fexc-asm32`, `fexc-precision-32`); `correctness`; the gate before the merge.
- **`sample-roles`** changes by design (role sequences of idioms with a branch): revised with the classifier, which gives
  pre-words the `flags` role so the table stays comparable.
- **`fexc-replay`:** with `--exit-flags` off, every reply byte-identical (the case as it is, the stream recorded with the
  argument off). With it on, a new comparator in `tools/fexc/replay.py` (like `only_moves`, 259-340): a reply may differ
  only by removed body words the classifier calls `flags` or PF moves, pre-words before exits with their counts, the offsets
  those shift and (version 1) the header byte; its own mutation check (one change elsewhere per reply must be reported).
- **New, version 0:** `exit-flags-shapes` (static, beside `sample-roles`): an idiom corpus (cmp/test/and/add/sub/inc/dec
  with Jcc, operands clobbered or not, 8/16/32/64-bit, AH-style operands, 32-bit code) with the expected pre-words and body
  words written by hand, and a semantic check: each unit compiled with and without the argument runs natively from entry to
  every exit (`tools/fexc/exec_words.c`, extended to end at an exit's `svc` and report NZCV) on random registers, and the
  six flags by `guest_flags` must be equal at every exit. Mutations: a recipe reading a clobbered register; the exit
  block's `cfinv` dropped; the cleanup not removing a redundant restore (caught by the hand word counts). Admission unit
  tests: pre-words writing a guest register, touching memory, branching, or a branch entering an exit after its first
  pre-word are refused (mutation: the check removed). `gen_slice2.py` gains "flag-exit" templates: a taken edge to a
  label whose first instruction reads PF, AF or CF (SETcc, ADC, LAHF, PUSHF, `jp`, `ja`) while the fall-through rewrites
  them, crossed with the `loopfaults` page mutations (mutation: fexc drops the pre-words but keeps the body deletion).
- **New, version 1:** entry-need hand cases in `exit-flags-shapes` (a fault point first: all; a kill first: none; LAHF
  first: S, Z, A, P, C). Link-guard unit tests in the resident's cache tests (skip only when the need and the mask are
  disjoint, never through a checked link, never into a step, system or breakpoint block). A system case `flag-exits-irq`
  (`tests/system`): loops shaped like the two `zstd` units under a short-period LAPIC timer at 1 and 4 vCPUs; the handler
  logs the frame's RIP and RFLAGS with the registers, and the judge recomputes the defined flags expected at that RIP with
  `flags.py`'s rules; it must see at least 100 interrupts at a skip-entered block's first RIP (coverage, counted). Its
  mutations: a first-word doorbell stopping at once; arm not restoring the first pre-word; the guard ignoring the need.
  `loopfaults` templates with the fault in the target's first instruction (mutation: guard off).

## 10. Version 2 (a lead, not planned)

Fault-site recipes: a site record names where PF and AF can be recomputed (a guest register holding the result, the
operand registers) and fexc drops the words when only fault points read them; `bank_at` recomputes. `sqlite`'s flag words
are mostly of this kind. Needs a site-record field, a recipe check in admission and the fault families extended; about as
large as versions 0 and 1 together. Decided by the end-of-round profile.

## 11. Estimate (agent days, low confidence)

| Part | Days |
| --- | ---: |
| v0 fexc (fork: CF carry, recipes, cleanup; `--exit-flags`) | 1.5 |
| v0 admission, trampolines, split | 1.5 |
| v0 tests (`exit-flags-shapes` with `exec_words`, admission tests, `gen_slice2` templates, replay comparator) | 2 |
| kill-test case `flag-words-zstd` | 0.5 |
| v1 fexc entry need, unit-end and self-exit pre-words | 0.5 |
| v1 resident (guard, arm, first-word rule, counters) | 1.5 |
| v1 tests (`flag-exits-irq`, guard tests, mutations) | 1.5 |
| total | 9 |

Plus VM time: the families above at each version, the gate once, one `guest-zstd-sample` run. Expected gain, from the
samples (an upper bound: a removed word's samples partly move to the next one, and the hot `cfinv` sits before a
data-dependent `je`): `zstd` compress 2-5% of EL0 time for both versions, `build` and the make step about 1%, `sqlite` about
nothing until version 2.

## 12. Risks

- CF carried across a non-rectified CondJump: a wrong CF form at a site in the fall-through gives a wrong CF at a fault
  (`loopfaults` and the new templates cover it).
- A recipe that reads a register written after the flag's writer: the semantic check at every exit is the defence.
- The trampoline layout change touches every split block, links, arming and `native_offset`: `k1-*`, `pic-*`, `x3-*` and the
  armed-stop tests must pass, and they do not test pre-words unless the corpora have them (the new templates do).
- Version 1's skip state is inexact by design until the target's kill: any observation point missed in section 6 is a
  wrong-flags bug. The mutation-backed `flag-exits-irq` and `loopfaults` cases are the check; a review of the list by an
  independent agent before building version 1.
- Undefined-flag values become link-dependent in version 1 (section 5).

# Indirect jumps and calls: a branch per site, then a one-entry inline cache (plan)

Status: plan, November 23, 2026, on `m2-sampler` at d7a662c7. Design only: nothing is built or run for it, and it has not
been reviewed; an independent design review comes before any code (AGENTS.md). The slot forms and the words appended to a
block are described once, in the [return prediction plan](return-prediction-plan.md) sections 4.1 and 4.2, and are built by
whichever of the two plans goes first. That plan's section 3 lists what was read in the code; this one adds what concerns
indirect sites.

## 1. Why

An indirect `jmp` or `call` leaves its block through the computed exit `mov x0, xR; b <slot>`, and the context's one slot
ends in `br x1` (crates/runtime/src/cache.rs:682-699). Every indirect site of the context, and every return, shares that
one branch address. Chunk 28 (an experiment, before the data tables) measured 1-2.4 ns per exit with a predictable target and
4.9-5.4 ns with a random one, 0 ns at a monomorphic site through a compare and a direct branch, and most of the dispatcher's
samples on the landing word ([guest-code review](guest-code-review-20261114.md) sections 3 and 6).

Two more streams reach the same `br`:

- **Checked links.** A literal exit of shared code to another page keeps its page-relative move and branches to the slot
  (cache.rs:2300-2312). Its target is fixed per instance: within one process such a site has one target, yet it is
  predicted through the shared branch. Shared code is most user code of multi-process workloads (`build`, the configure and
  make steps; [pic32 findings](m2-pic32-findings.md)).
- **Interpreters.** A threaded interpreter (LuaJIT's, `guest-luajit-interp`) ends each handler with its own indirect jump,
  so that each jump has its own predictor entry natively; through one shared branch that benefit is gone.

The census (bench cases `guest-<b>-census`, QEMU with the census plugin on the M2 Max, run 1 of each benchmark), user code,
per address space, execution-weighted share of each indirect site's main target: `build` 63% (top two 73%), `luajit` 75%,
`sqlite` 55%, `startup32` 55%, `hackbench`, `zstd`, `scimark` 97-100%. These are lower bounds (the plugin keeps 4 targets per
site; 21-35% overflowed on `build`, `luajit`, `sqlite`, `startup32`). With targets relative to the site (what a cache in
shared code can compare), `build` 57%. Checked links are not in the census: they are natemu's, not the guest's.

## 2. The design

**Step 1, a branch per site.** The site's `b` goes to two words appended to its block: `bl <its context's lookup form>; br
x1`. The lookup form probes the table as the slot does and returns with x1 = the target (or the miss's address; section 4.1
of the return plan). The `bl`/`ret` pair is predicted perfectly by the return stack, and the site's own `br x1` gets a
predictor entry of its own. No cache, no fill, no patching, no compare: the target is the table's, as now. For an indirect
call with return prediction built the words are `bl lookup; blr x1; pad` (the return plan's pad after `blr`); without it,
`bl lookup; br x1`. A checked link's `svc` word becomes `b` to the same two words.

**Step 2, a one-entry inline cache.** The appended words grow a compare against one remembered target and a direct branch
to it, before the step-1 words:

```
movz/movk x1, #C           2 words (32-bit), 3 (user or kernel 64-bit, `movn` for the kernel's)
eor   x1, x1, x0
cbnz  x1, L
b     <C's full entry>     a link: a link record, undone like any other
L: bl <lookup form>; br x1  step 1
```

In shared code the compare is page-relative: `ldr x1, [x28, #PAGE_BASE]; sub x1, x0, x1; sub x1, x1, #d; cbnz x1, L; b
<template's full entry>`, for a target on the source's own page and frames only (the local-link rule, cache.rs:2318-2321),
whose template is right for every instance. Other targets of shared code are never cached; step 1 serves them.

An indirect call under return prediction has two `bl` sites in step 2 (the hit's `bl <entry>` and the miss path's `blr x1`),
each needing its own pad, while the table holds one pad per return address: the entry names the hit's pad, and a return after
a miss is mispredicted. Whether step 2 is worth that for calls, or is kept to jumps and checked links, is the kill-test's
call variant's question (section 5).

## 3. Details

### 3.1 Sites

Computed exits that are not returns (returns go to the return plan's return form): `jmp r/m` and `call r/m`, near; and, in
shared code, checked links. Admission's hint says which computed exit is a call (FF /2), as for the return plan (its section
4.3); a wrong hint changes only the form.

### 3.2 Registers, flags, the return stack

The appended words use x0 (the target, never written), x1 and x30 (temporaries at a block boundary: structure.rs:61, the
dispatcher's x0-x2 rule, gates.S:253-257) and write no flags. The lookup's `bl`/`ret` are balanced, so they leave the guest's
return-stack entries as they found them; the return plan's predictions are not disturbed.

### 3.3 Filling (step 2)

An empty cache's first word is `bl DISPATCH_MISS`: the first execution enters the resident with x0 = the target and x30 = the
cache's second word, which names the site (`block_at(x30)` and the record's tail roles). The resident finds or makes the
target's block as for any miss, writes the compare and the link to it (absolute: the target's full entry; shared: only if the
target is local, else it writes the first word as `b L`, step 1 forever), records the link, synchronizes once and enters
the target. The cache is filled once: a later miss goes to step 1 and changes nothing. It becomes empty again when its link
is undone (the target died or stopped being current, a CR3 load for a target in another page, a flush), and then fills with
the next target it meets. So a site's hit rate is the share of the first target it meets after each emptying; the census's
main-target shares bound it. A miss-counting refill (a counter per site in resident data) is left for later.

### 3.4 Unlinking

The cache's `b <entry>` is a link from the site's block to the target, recorded with its original word (the empty form's
`bl DISPATCH_MISS`, or `b L`). `prune` (cache.rs:3174) undoes it by the existing rules of its segment; nothing new decides
when. Once undone, the compare words left behind are dead: both outcomes of `cbnz` reach step 1 or the resident.

### 3.5 Other vCPUs

None run or patch this code: each vCPU's code area is its own (cache.rs:13-14). A peer's write to a code page reaches this
vCPU as a posted invalidation it takes itself (cache.rs:2866-2900); pool templates are copied into the vCPU's own area before
they run. A fill or an unlink happens while this vCPU is in the resident, so its several words need no atomicity, only one
synchronization before the vCPU returns to EL0.

### 3.6 Code synchronization on fill

Each fill writes 5-8 words and synchronizes them: `dc cvau` is skipped when CTR_EL0.IDC is set, `ic ivau` when DIC is set,
then `dsb ish; isb` (cache.rs:2070-2105; the resident reads CTR_EL0 once at start). About 200 ns on an M4 (layout page
section 2), plus the trip into the resident (microseconds). One fill per site per emptying: the same order as the trips that
link literal exits today. Step 1 writes code only at placement, with the block.

### 3.7 Containment

EL0 gains nothing it can write. The cache's target is a link to an admitted block's full entry in the site's context, made
and undone by the resident with a record; the compare guarantees x0 = C (absolute) or x0 = page base + d (shared). The
shared form reads `PAGE_BASE` in the guest-state page, which the guest can overwrite (the G1 deviation, [boot features
plan](boot-features-plan.md)); shared code's own literal moves and local links already rely on it in the same way, so a guest
that overwrites it misdirects only its own execution, into admitted template code at a page it chose, as today. Step 1's
target is the table's. Nothing here is trusted from guest-writable memory beyond what shared code already reads.

### 3.8 Stale state

| Event | What happens |
| --- | --- |
| Flush | everything starts empty |
| The target dies or stops being current | `prune` undoes the cache's link; the cache is empty |
| The site's block dies | its links are undone as its other links are; its bytes stay until a flush |
| CR3 load | links from another page into non-global targets undone, as now; step 1 has no state |
| Another code context or CPL | the site's block runs only in its context; its lookup form is its context's slot |

### 3.9 Exactness

None changes: the exit's guest effects (the branch-target check, a call's push) are done before the site's `b`, as now.

### 3.10 32-bit code, hot/cold, extended blocks

32-bit targets compare in two words. The appended words follow the block's whole code in the cold side; a split block's hot
trampoline to its computed exit is pointed at them directly. A block has one computed exit and up to four link points, so at
most five sites; only checked links and the computed exit get appended words. Extended blocks change nothing.

### 3.11 Stops and the sampler

At every appended word x0 is the target, so an interrupt there stops at once with the next RIP x0, as in the slot area
(execution.rs:2583). The sampler's `TAIL` place (return plan section 4.11) carries the word's role (lookup pair, cache
compare, cache branch); the lookup form's own samples stay `DISPATCH`.

### 3.12 Admission and fexc

fexc does not change; admission returns the call/jump hint only. All appended words are resident code. A fexc variant
(placeholder words emitted after each computed exit) would add admission rules and change every answer and the cache key for
no gain.

### 3.13 Why it should not lose

Step 1 adds a `bl` and a `ret` (both predicted) to every exit: about half a nanosecond (estimate) where the shared branch
already predicted the target, which a branch history can do for a site with a short pattern. It gains where the shared branch
mispredicts and the site's own does not. Step 2 saves the probe on a hit and costs the compare on a miss (three to five
words), plus the fills. The kill-test measures all three against today.

## 4. Smallest useful version first

Step 1 for computed exits and checked links, in absolute and shared code alike: it compares nothing, so the page-relative
question does not arise. With it come the slot's lookup form and the appended words, if the return plan has not built them.
Left for later: step 2 (absolute first, then shared local targets), a refill policy, caches in the hot stream.

## 5. Kill-test (before any natemu change, under an hour)

The `host-dispatch-micro` case of the return plan (its section 6), with indirect variants: V0 (today: every site `b slot`,
the slot's `br`), step 1 (`bl lookup; br x1` per site), step 2 (the cache, filled with each site's main target, step 1 behind
it), and the same three for indirect calls whose callee returns through the return plan's form (pads after `blr` and after
the cache's `bl`). Sites: 64, each with 4 targets drawn with the census's main-target shares (0.55, 0.63, 0.75, 0.97, and 1.0), and an
interpreter pattern (64 handlers, each jumping to the next by a fixed Markov table, as a bytecode stream does). Kill step 1
if it saves less than 1 ns per exit at 0.63 and 0.75 or loses more than 0.5 ns at 1.0; build step 2 only if it saves 1 ns or
more per exit over step 1 at 0.75. After step 1 is built, an early check in the VM: `guest-luajit-interp` and `guest-luajit`,
base and fix, one round; stop and look again if neither moves beyond its spread.

## 6. Tests

Existing families: `faults` (indirect `jmp`/`call` forms, the branch-target #GP before the push, at block sizes default and
1), `system` (`frame-randomization`; `run_k1.py` and the relink mutations for links), `smp` (`coherence`, `contain`,
`privilege`), `programs`, `correctness`, `boot`, `io`, the Rust units, the `link-check` resident build.

New cases, each shown to fail when it should:

| Case | Shows | Fails under the mutation |
| --- | --- | --- |
| `system` `indirect-sites`: a jump table and a call table with sites of 1, 4 and 64 targets, in 64-bit and i386 builds, in a shared library mapped at two addresses by two processes | every target reached; counters show per-site exits and, in step 2, hits | step 2 caching a non-local target in shared code; the compare word for a 64-bit target truncated to 32 bits |
| `system` `indirect-death`: targets rewritten (SMC) and remapped (CR3) between executions, at 1 and 4 vCPUs | no cached link survives its target | `prune` skipping cache links (`link-check` halts) |
| `system` `indirect-stop`: interrupts landing on each appended word | the stop is at x0's RIP | the stop rule at appended words removed |
| Rust unit: the forms' words (a decoder test over every form, both widths, both compare kinds) | the words branch where they should | each form's branch offset off by one word |

While iterating: `tools/natemu-test run tests/system -k 'indirect- or relink'`, `tests/faults -k 'jmp or call'`, the Rust
units; the affected tier (`tools/natemu-test run`) at the end of the task, since every computed exit changes.

## 7. Runs (fixed budget)

On the M2 Max; its numbers are evidence for the commit message, never the benchmarks page.

| Run | What | Budget |
| --- | --- | --- |
| Kill-test | `host-dispatch-micro` (shared with the return plan) | in its 10 minutes |
| Census addition | per-site last-target and first-target models for indirect sites (what step 1 and step 2 predict), in the same plugin change as the return plan's | in its hour |
| In-VM early check | `guest-luajit-interp`, `guest-luajit`, base and fix, one round | 20 min |
| A/B, step 1 | base and fix alternating, three rounds (`TESTKIT_BENCH_AT`): `guest-luajit-interp`, `guest-luajit`, `guest-build`, `guest-sqlite`, `guest-startup32`; the noise is the spread across rounds | about 1.5 h |
| A/B, step 2 | if built: the same cases, step 1 as the base | about 1.5 h |
| Profile | one `guest-startup32-profile` run with the fix: the slot's non-return share should fall | 10 min |
| Gate | once, over both plans, at the end | (the return plan's) |
| Benchmarks page | only if a page number moves beyond its noise: those cases on the M4, base and fix alternating, three rounds | owner schedules the M4 |

## 8. Estimate (agent hours, low confidence)

Step 1, if the return plan has built the appended words and slot forms: lookup form 1, sites and checked links 2, stops and
sampler roles 1, tests and mutations 2, review fixes 1: about 7 hours (about 11 if it goes first and builds them). Step 2:
cache forms 2, fill path 3, link records 2, shared compare 2, tests 3, review 2: about 14 hours.

## 9. Risks

- The shared branch already predicts most sites through its history: step 1 then costs half a nanosecond per exit for
  nothing. The kill-test's V0 at 1.0 and the interpreter pattern decide.
- Appended words on every computed exit and checked link grow the code area (the cache's limit): 8-12 bytes per such block,
  more flushes; the A/B reports flush counts.
- Step 2's sticky fill keeps a minority target on a site whose first target was rare; its hit rate may sit well below the
  census share.
- Step 2's link records add to the record area's load (reclaims), and its fills add trips into the resident.
- The new resident paths (stops at appended words, the fill) sit beside the linker's subtlest code.

## 10. Decision: the sampler number

Step 1: build it when the slot's samples whose target fails the call test (`DISPATCH`, call test no: indirect exits and
checked links) are 3% or more of EL0 samples on one of `guest-luajit-interp`, `guest-luajit`, `guest-build`, `guest-sqlite`,
`guest-startup32`, and the kill-test passes. Step 2: the [sampler plan](el0-sampler-plan.md)'s rule (line 274: the slot and
its landing words 8% or more of EL0 samples on `build` or a make step, and the main-target share 60% or more), read after step
1 on the lookup form, the sites' `br` and their landing words; today `build` (63%) and `luajit` (75%) meet the share and
`sqlite` and `startup32` (55%) do not. Under 3%: recorded and left.

## 11. What is verified and what is not

Read in the code: as the return plan's section 12, and checked links' form and the local-link rule (cache.rs:2300-2321),
`PAGE_BASE` in the guest-state page and the shared full entry (abi execution.rs:1350-1358, cache.rs:1506-1540), the posted
invalidations (cache.rs:2866-2900), the code synchronization (cache.rs:2070-2105). Not verified: how Apple cores predict a
shared indirect branch with history (the kill-test measures it); the cost of a fill on the M2 Max; the share of exits that are
checked links (the profile will show it once the forms are apart).

## 12. Open questions for the owner

1. Step 1 is not an inline cache but a branch per site, the cache coming second if the kill-test asks for it: agreed?
2. Checked links of shared code in this plan (they are natemu's exits, not the guest's indirect branches): agreed?
3. Step 2's fill once per emptying, no refill counter: enough for a first version?

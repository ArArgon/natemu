# Indirect jumps and calls: a branch per site, then a one-entry inline cache (plan)

Status: plan, November 23, 2026, on `m2-sampler`. Design only: nothing is built or run for it. Revision 2, after an
independent design review (section 12): step 1 accepted with fixes, taken in; step 2 deferred. This plan goes first: it builds
what the [return prediction plan](return-prediction-plan.md) then uses (the slot's forms, the words appended to a block, `blr`
at indirect calls). Two plans, not one, because each has its own sampler number, benchmarks and A/B.

## 1. Why

An indirect `jmp` or `call` leaves its block through the computed exit `mov x0, xR; b <slot>`, and the context's one slot
ends in `br x1` (crates/runtime/src/cache.rs:682-699). Every indirect site of the context, and every return, shares that
branch address. Chunk 28 (an experiment, before the data tables) measured 1-2.4 ns per exit with a predictable target, 4.9-5.4
ns with a random one, 0 ns at a monomorphic site through a compare and a direct branch, and most of the dispatcher's samples on
the landing word ([guest-code review](guest-code-review-20261114.md) sections 3 and 6). Two more streams reach the same `br`:

- **Checked links.** A literal exit of shared code to another page keeps its page-relative move and branches to the slot
  (cache.rs:2300-2312). Within one process its target is fixed, yet it is predicted through the shared branch. Shared code is
  most user code of multi-process workloads ([pic32 findings](m2-pic32-findings.md)).
- **Interpreters.** A threaded interpreter (`guest-luajit-interp`) ends each handler with its own indirect jump so that each
  has its own predictor entry natively; through one shared branch that is lost.

The census (bench cases `guest-<b>-census`, QEMU with the census plugin on the M2 Max, run 1 of each benchmark), user code, per
address space, execution-weighted share of each site's main target: `build` 63% (top two 73%), `luajit` 75%, `sqlite` 55%,
`startup32` 55%, `hackbench`, `zstd`, `scimark` 97-100%; lower bounds (4 targets kept per site; 21-35% overflow on the first
four). With targets relative to the site, `build` 57%. Checked links are natemu's exits and are not in the census.

## 2. What exists (read in the code)

- Each vCPU's code area, tables and slots are its own: "Blocks and L1 are this vCPU's own ... so a patch stops no other vCPU"
  (cache.rs:13-14; crates/abi/src/execution.rs:250-257). Placement is a bump fill (`reserve`, cache.rs:105-112).
- The slot area: 128 slots of 128 bytes (abi execution.rs:282-287); a slot is bound to a context's table (`bind_slot`,
  `write_slot`, cache.rs:663-735); a computed exit's `b` is relocated to `dispatcher_for(context)` at placement (cache.rs:738,
  `relocate` at 2124).
- Data tables (`area::DL1`, abi execution.rs:341-357): (RIP, target) pairs, EL0 read-only, per privilege class, each held by one
  context; filled by `fill_l1` with the owner's full entry (cache.rs:789-834).
- Exits (crates/host/src/admission/structure.rs:22-30): literal exits are link points entered at their first word; one
  computed exit at most (structure.rs:998-1013); four link points at most (abi execution.rs:2522). "x30 is an ordinary
  allocator temporary: no `bl` is admitted" (structure.rs:61): nothing lives in x30 at a block boundary.
- Shared code: an instance has no code (`instance`, cache.rs:1407); the template's full entry takes x0 = the RIP and stores the
  page base (abi execution.rs:1350-1358); links out of it are local (same page, direct) or checked (cache.rs:2300-2321).
- Stops: in the slot area the vCPU stops at once with the next RIP in x0 (crates/runtime/src/execution.rs:2583); inside a dead
  block it halts `BAD_STOP` (execution.rs:2601).
- The sampler classes a slot-area PC as `DISPATCH` with x0 and a call test (crates/abi/src/sample.rs:156-159, 186-192).

## 3. The design

### 3.1 The slot's forms (shared with the return plan)

Each bound slot gets three forms with the same probe:

| Form | Ends in | Used by |
| --- | --- | --- |
| ordinary (today's) | `br x1` | anything not changed |
| lookup | `ret` with x1 = the target; miss: `adr x1, DISPATCH_MISS; ret` | step 1's sites |
| return | `ret x1`; miss: `adr x1, DISPATCH_MISS; ret x1` | the return plan |

Three forms of 13 words need 256-byte slots: `DISPATCH_BYTES` grows to 32 KiB (the probes and `HEAD_BYTES` move; constants
with asserts). An unbound slot's lookup form is `adr x1, DISPATCH_MISS; ret`. `write_slot` writes all three under one
synchronization. No form writes NZCV (gates.S:253-257).

### 3.2 Words appended to a block (shared with the return plan)

The resident appends a few words of its own after a block's placed code (cold side), as it writes the hot copy's trampolines.
They are resident code, not admitted code, with no guest semantics. The record gains their extent and roles; `block_at` covers
them. An interrupt on one is a block boundary and stops at once, with the next RIP x0 for every word of this plan; the rule is
checked before the dead-block filter (execution.rs:2601), as the slot rule is. The sampler gets a place `TAIL` (8) with the
word's role.

### 3.3 Step 1: a branch per site

| Site | Appended words |
| --- | --- |
| `jmp r/m`, a checked link that is not a call | `bl <lookup form>; br x1` |
| `call r/m`, a checked link that is a call (shared code) | `bl <lookup form>; blr x1;` pad |

The site's `b` (or the checked link's `svc` word) goes to them; a split block's hot trampoline goes to them directly. The
lookup's `bl`/`ret` pair is predicted by the return stack, and the site's own `br`/`blr` gets a predictor entry of its own. The
target is the table's, as now: no cache, no fill, no patching, no compare, so absolute and shared code are treated alike.

Calls use `blr` from the start, so that every indirect call pushes on the return stack: the return plan needs every call to
push (its section 4.1). Until that plan is built nothing pops these pushes (returns still end in `br`); the lookup's pairs nest
above them and stay predicted, and the stack simply wraps. The pad word is `b DISPATCH_MISS`; nothing reaches it until the
return plan fills entries naming it. A lookup miss reaches the miss through the site's `br`/`blr x1`, with x0 the target; a
miss through `blr` has pushed, and the resident enters the target directly.

Admission returns which computed exit and which link point is a call (FF /2, E8) as a hint beside the link points; a wrong hint
changes only the form, never the target.

### 3.4 Registers, flags, exactness, containment

The words use x0 (the target, never written), x1 and x30 (temporaries at a block boundary) and write no flags. The exit's guest
effects (the branch-target check, a call's push) are done before the site's `b`, as now. EL0 gains no writable and no new
readable memory, and every target is the table's: containment is today's.

### 3.5 Stale state

Step 1 holds no state: a flush, a death, a CR3 load or a context change act on the tables as now. A dead block's appended
words stay until a flush (bump fill) and are reached only from its own exits.

### 3.6 32-bit code, hot/cold, extended blocks

Nothing differs for 32-bit code. A block has one computed exit and up to four link points; only the computed exit and checked
links get appended words. Extended blocks change nothing.

### 3.7 Why it should not lose

Step 1 adds a predicted `bl`/`ret` to every exit, about half a nanosecond (estimate), where the shared branch already predicted
the target (a history-based predictor can, for a short pattern). It gains where the shared branch mispredicts and the site's own
does not. The kill-test measures both.

### 3.8 Step 2, deferred: a one-entry inline cache

Built only if the kill-test and the sampler ask for it after step 1. The appended words grow, before step 1's:

```
movz/movk x1, #C           2 words (32-bit), 3 (64-bit; `movn` for kernel addresses)
eor   x1, x1, x0
cbnz  x1, L
b     <C's full entry>     a link with a record, undone by `prune` (cache.rs:3174) like any other
L: step 1's words
```

- **Shared code:** a page-relative compare (`ldr x1, [x28, #PAGE_BASE]; sub x1, x0, x1; sub x1, x1, #d; cbnz x1, L; b
  <template's full entry>`), only for a target on the source's own page and frames (the local-link rule, cache.rs:2318-2321).
  `PAGE_BASE` is in the guest-state page, which the guest can overwrite (G1, [boot features plan](boot-features-plan.md));
  shared code's own moves and local links already trust it the same way, so an overwrite misdirects only the guest's own
  execution into admitted code, as today.
- **Filling:** an empty cache's first word is `bl DISPATCH_MISS`; the resident takes the miss with x0 = the target and x30 = the
  cache's next word. x30 is guest-influenced (structure.rs:61: any block may write it), so the resident fills only if x30 lies in
  the appended words of a live block, at a cache role, that cache is empty, and the block's context is the current one;
  otherwise it serves an ordinary miss. It fills once per emptying (the link undone): no refill counter.
- **Sync:** one synchronization per fill (`dc cvau` skipped with CTR_EL0.IDC, `ic ivau` with DIC, then `dsb ish; isb`;
  cache.rs:2070-2105), about 200 ns on an M4 (layout page section 2), plus the trip. Other vCPUs neither run nor patch this code
  (cache.rs:13-14; peers post invalidations, cache.rs:2866-2900), and the vCPU is in the resident, so the words need no
  atomicity.
- **Calls:** the hit's `bl <entry>` and the miss's `blr x1` would need a pad each while the table holds one per return address;
  the kill-test's call variant says whether step 2 is kept to jumps and checked links.

## 4. Smallest useful version first

Step 1 for computed exits and checked links, in absolute and shared code, with `blr` for calls; with it the slot's lookup and
return forms (the return form unused until the return plan), the appended words, their stop rule and the sampler place. Order:
this, then the return plan on top, sharing the lookup form; step 2 later, if at all.

## 5. Kill-test

**Host part (under an hour, before any natemu change):** `host-dispatch-micro`, a case in `tests/bench` (a generator writing the
assembly and a small C driver timing with `mach_absolute_time`, built with the Xcode command-line tools the README requires;
exclusive; five rounds, variants alternating; ns per exit with the spread across rounds). Labelled host-native, run on the M2
Max, never on the benchmarks page. The return plan's variants share it. Indirect variants: V0 (every site `b slot`, the slot's
`br`), step 1, step 2 (filled with each site's main target); for jumps and for calls. Sites: 64, each with 4 targets at the
census's main-target shares (0.55, 0.63, 0.75, 0.97, 1.0), visited in a short period (64) and a long one (64 Ki or more), and an
interpreter pattern (64 handlers jumping by a fixed Markov table). Kill step 1 if it saves less than 1 ns per exit at 0.63 and
0.75 in the long period, or loses more than 0.5 ns at 1.0; step 2 is considered only if it saves 1 ns or more over step 1 at
0.75.

**In-VM check (after step 1 is built):** `guest-luajit-interp` and `guest-luajit`, base and fix, one round on the M2 Max; stop
and look again if neither moves beyond its spread.

## 6. Tests

Existing families: `faults` (indirect `jmp`/`call` forms, the branch-target #GP before the push, block sizes default and 1),
`system` (`frame-randomization` and its `tables-*` checks; `run_k1.py` and the relink mutations), `smp` (`coherence`,
`contain`, `privilege`), `programs`, `correctness`, `boot`, `io`, the Rust units, the `link-check` build.

New cases, each shown to fail when it should:

| Case | Shows | Fails under the mutation |
| --- | --- | --- |
| `system` `indirect-sites`, 64-bit and i386 builds: a jump table and a call table with sites of 1, 4 and 64 targets, in a shared library mapped at two addresses by two processes | every target reached; counters show per-site exits | a call site given `br` (the push-count check); the lookup form's miss returning a stale x1 |
| `system` `indirect-stop`: interrupts landing on each appended word, including in a block killed meanwhile | the stop is at x0's RIP, no `BAD_STOP` | the appended-word rule after the dead filter |
| Rust unit: every form's words (decoder test, both widths) | the words branch where they should | a branch offset off by one word |

While iterating: `tools/natemu-test run tests/system -k 'indirect- or relink'`, `tests/faults -k 'jmp or call'`, the Rust units;
the affected tier (`tools/natemu-test run`) at the end, since every computed exit and the slots change.

## 7. Runs (fixed budget)

| Run | What | Budget |
| --- | --- | --- |
| Host kill-test | `host-dispatch-micro` (indirect and return variants) | 10 min exclusive |
| In-VM check | `guest-luajit-interp`, `guest-luajit`, base and fix | 20 min |
| Iteration | narrow runs; the affected tier once | no heavy runs |
| A/B | M2 Max, base and fix alternating, three rounds (`TESTKIT_BENCH_AT`): `guest-luajit-interp`, `guest-luajit`, `guest-build`, `guest-sqlite`, `guest-startup32`; noise is the spread across rounds; flush counts reported | about 1.5 h |
| Profile | `guest-startup32-profile` with the fix: the slot's non-return share should fall, the site words' share show | 10 min |
| Gate | once, over both plans, after the return plan | one gate |
| Benchmarks page | only if a number moves beyond its noise: those cases on the M4, alternating, three rounds | owner schedules the M4 |

## 8. Estimate (agent hours, low confidence)

Slot forms 2, appended words and records 2.5, sites and checked links 2, stops and sampler place 1, the kill-test case 1, tests
and mutations 2, review fixes 1: about 11 hours. Step 2, if ever: about 14 more.

## 9. Risks

- The shared branch already predicts most sites through its history: step 1 costs half a nanosecond per exit for nothing (the
  kill-test's 1.0 share and short period).
- Appended words on every computed exit and checked link grow the code area, the cache's limit: 8-12 bytes per such block; the
  A/B reports flushes.
- `blr` pushes with no pop until the return plan lands: harmless by design (3.3), but a measured loss would show it.
- The stop rule at appended words is new resident code next to the linker's.

## 10. Decision: the sampler number

Step 1: build it when the slot's samples whose target fails the call test (`DISPATCH`, call test no: indirect exits and checked
links) are 3% or more of EL0 samples on a profiled benchmark, and the host kill-test passes. Existing profile cases:
`guest-startup32-profile`, `guest-cray32-small-profile`, `guest-hackbench32-profile`, `guest-scimark*-profile`; `luajit`,
`luajit-interp` and `sqlite` need a `-profile` case each (one case in `tests/bench/family.toml`, as the others). Step 2: the
[sampler plan](el0-sampler-plan.md)'s rule (line 274: slot and landing words 8% or more on `build` or a make step, main-target
share 60% or more), read after step 1 on the lookup form, the sites' branches and their landing words. Under 3%: recorded and
left.

## 11. Verified and not

Read in the code: everything cited with file and line. Not verified: how Apple cores predict a shared indirect branch with
history (the kill-test measures it); a fill's cost on the M2 Max; the share of slot exits that are checked links (the profile
shows it once the forms are apart).

Defaults taken from the review (no longer open questions): a branch per site first, the cache second; checked links included;
appended resident words instead of a fexc change; the slot area at 32 KiB; `host-dispatch-micro` as a bench case; step 2's fill
once per emptying, with x30 validated.

## 12. Review

Independent design review (Fable, November 23) of revision 1: step 1 accept with fixes, step 2 defer. Taken in: `blr` at
indirect calls from step 1 on, so every call pushes (the return plan's balance rule), and this plan first; step 2's x30
validated before a fill (live block, cache role, empty, current context); the stop rule before the dead-block filter; the
decision read from profile cases; the census addition dropped; the kill-test's short and long periods; the estimate.

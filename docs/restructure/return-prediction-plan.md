# Returns predicted by the Arm return stack (plan)

Status: the smallest version is built (November 24, 2026, section 14: kill-test, build, tests). Plan of November 23, revision 3: revision 2 (the
return stack balanced on every path, pads without link records, a host kill-test for mechanism and an in-VM one for the
decision, the decision read on benchmarks the built version covers) was accepted with fixes by the independent re-review, and
the fixes are taken in (section 13). Owner decision (November 23): the pad is an "external guarded entry" in INV-03-01's sense,
under the invariant of section 4.2, now written into that rule (section 4.3). It comes second: the [indirect plan](indirect-cache-plan.md)'s step 1 goes first and
builds what both use (the slot's lookup form, the words appended to a block, `blr` at indirect calls); this plan adds to it.

## 1. Why

Every guest `ret` leaves its block through a computed exit, `mov x0, xR; b <slot>`; the slot (one per code context) probes
the context's data table (`area::DL1`) and ends in `br x1` (`cache::write_slot`, crates/runtime/src/cache.rs:663-715, the 13
words at 682-699). One `br` serves every return of the context. The [layout page](translated-code-layout.md) (line 117) puts a
return at about 9 ns more than a jump-linked block; chunk 28 (an experiment, before the data tables) measured an exit at
1-2.4 ns with a predictable target and 4.9-5.4 ns with a random one, most of the dispatcher's samples on the landing word
([guest-code review](guest-code-review-20261114.md) section 3, item 1): the cost is the misprediction.

The census (bench cases `guest-<b>-census`, QEMU with the census plugin on the M2 Max, run 1 of each benchmark): 9 M
(`scimark32-sse2`) to 720 M (`startup32`) returns per run, about 13-100 M per second at natemu's 1-vCPU times; a model return
stack per vCPU with user and kernel kept apart hits 99.7% or more at depth 16 (97% at depth 8). `hackbench`'s returns are 90%
kernel. A hardware return stack is one per core and mixes user, kernel and the resident, so the census is an upper bound.

An earlier return-pairing experiment lost time on 64-bit code ([task 4 findings](m2-task4-findings.md), lines 447-453); its
record was not found. Section 4.9 lists what can make pairing lose and what this design does about each.

## 2. The design in one paragraph

Every call is made with `bl` or `blr`, so the core's return-address stack (the RAS) holds the host address of the word after
it, a *pad*; every return goes to a slot form ending in `ret x1`, so it pops. The return form probes the same data table as
today, and for a return address R in absolute code the table holds R's pad, whose word is a branch to R's block. So the
target comes from the resident-written table as now, the RAS only predicts it, and when calls and returns pair the return is
predicted. Nothing new is stored at a call and nothing EL0 can write is trusted. The resident does it alone.

## 3. What exists (read in the code)

The indirect plan's section 2 lists the slot area, the per-vCPU code area, the tables and their owners, link points, x30 as
a temporary and shared code's checked links, with file and line. In addition:

- A table entry is emptied by its owner: `cut_one` when the owner dies or stops being current (cache.rs:3142-3168), the TLB
  path for listed entries whose owner is not current (cache.rs:1000-1012), `clear_l1` when a table changes hands
  (cache.rs:609), a flush. There are four tables (`L1_TABLES`, abi execution.rs:300), two per privilege class; `select_l1`
  clears one when a fifth context takes it (cache.rs:756-786).
- Link records are keyed by link point (`Block::out`, abi execution.rs:1256-1259); `relink` halts `BAD_LINK` unless the point
  names the target's RIP (cache.rs:3355-3360); links enter through `link_entry`, which may skip the target's x87 run test
  depending on the source (cache.rs:2280-2287). So a pad cannot be a link record: it is entered from the callee's return, not
  from its own block, and has no link point.
- Placement is a bump fill (`reserve`, cache.rs:105-112): a dead block's bytes stay until a flush.
- An interrupt inside a dead block halts `BAD_STOP` (`interrupted`, execution.rs:2601); in the slot area the vCPU stops at
  once with the next RIP in x0 (execution.rs:2583).
- Supervisor code is never shared (`shares`, abi execution.rs:1170-1176: user contexts only).

## 4. The design

### 4.1 Every call pushes, every return pops

The RAS does not resynchronize: a pop without a push, or a push without a pop, mispredicts every enclosing return until it
drains. So the design is balanced on every path, not only the common one:

| Path | How it pushes or pops |
| --- | --- |
| Direct call, linked | the link word becomes `b pair+0`; the call pair appended to the block is `pair+0: bl <T's entry>`, `pair+4:` the pad (section 4.2 for the link record); a split block's hot trampoline still goes to the link word, so only that word is patched |
| Indirect call, checked link that is a call (shared code) | the indirect plan's step 1: `bl lookup; blr x1;` pad |
| Local link that is a call (shared code) | the call pair, `bl` to the target's body |
| A call the resident continues before its `bl`/`blr` ran (its first execution, a stop at its exit, a target it cannot link); a miss reached through the site's `blr x1` has pushed already and is entered directly | the resident resumes at the *push stub*, `blr x1` with x1 = the target's full entry and x0 its RIP, never by entering the target itself |
| Return | the return form: both ways `ret x1`; its miss is `adr x1, DISPATCH_MISS; ret x1`, so a miss pops too |
| A return the resident continues before its `ret x1` ran (a stop at a ret exit); a miss has popped through its stub and is entered directly | the resident resumes at a *pop stub*, `ret x1` with x1 = the full entry it chose and x0 its RIP |
| Call to the next instruction (`call $+5; pop`) | linked as plain `b`: it never returns |

The stubs. The push stub is `blr x1` followed by `b DISPATCH_MISS`, the pop stub `ret x1`; both lie inside the slot area
(`DISPATCH .. DISPATCH + DISPATCH_BYTES`), so the slot area's stop rule (execution.rs:2583) covers them and a stop there
continues with x0. The push stub's `blr` pushes stub+4, not a pad: the return that matches it is mispredicted once, by
construction, and stub+4 is a safe target (`b DISPATCH_MISS` with x0 the RIP) although no table entry names it. A stop on the
push stub, at pair+0, or at the site's `blr x1` after its lookup returned has not pushed yet and continues through the push stub
again; a stop inside the lookup form leaves the lookup's own `bl` unpopped and is counted unbalanced. The stubs are never used
while `State::shadow_entry` is set (the block entered after a shadow-setting instruction must be entered at its first word): the
resident then enters the target directly and counts the path unbalanced.

Left unbalanced: the single-step slot's block, calls or returns the resident emulates (system cuts, the interpreter), and the
two counted cases above. Their count is reported; the kill-test prices an unbalanced call (section 6). Because every context's calls push, the return
form is used for every return. Admission's hint (the ending instruction is a near `ret`, a near call, `call $+5`) chooses the
forms; a wrong hint costs time, not correctness, since every form's target is the table's or a link's.

### 4.2 Pads, and the invariant that makes them safe

A pad is one resident word. It has no link record and no state of its own that anything trusts. The invariant:

> A table entry (R, p) with p a pad exists only while p's word is `b <full_entry(owner)>`, where the owner is R's current
> block in the table's context, which owns the entry.

- The call's own link (to T) stays an ordinary link record of its link point, with one change of form. The patched word at
  `link_at` is `b pair+0`, and pair+0 holds `bl <link_entry(source, T)>`; only the `link_at` word is the record's (the hot
  trampoline is not rewritten). Every path that checks or rewrites a record goes through pair+0 for a call point:
  `unlink_word`'s check (today `links_to(at, entry)`, cache.rs:2859-2860, which would return `None` on `b pair+0` and fail
  `suspend` at the first CR3 load, cache.rs:3294) compares `link_at` with `b pair+0` and pair+0's `bl` target with the entry;
  `relink` (cache.rs:3365), which writes a plain `b` today, writes pair+0's `bl` and then `b pair+0`, so a relinked call still
  pushes; `unlinked` (cache.rs:2252-2257) and `links_restored` (cache.rs:3425) keep comparing `link_at` with the admitted word,
  which unlinking restores; pair+0 is left as it is, unreachable.
- Filling writes the pad word (and synchronizes it), then the entry. It happens (a) when a direct call in absolute code is
  linked and R's block is current, and (b) on a miss for R, from a resident-private *hint* table (direct-mapped by R, 4 Ki
  entries of (caller block, pad offset, context)). A hint is used only if the caller is alive, its block end is R, the context
  is the table's, and the block the miss just found for R is the one the pad will name. `fill_l1` consults the hint, so an
  ordinary fill for R does not replace a valid pad.
- The pad always names `full_entry(owner)`: the full entry runs the x87 run test whoever returns (no `link_entry` skip).
- Emptying needs nothing new: entries are emptied by their owner (`cut_one`, the TLB path, `clear_l1`, a flush). After that
  the pad word is stale but unreachable: nothing in any block branches to it, and a return reaches it only through an entry.
  A speculative path through a stale RAS entry is squashed.
- When the caller dies, the resident empties the entries naming its pad (two slots for R in each of the four tables, those
  whose target is the pad; a table whose context is `NO_CONTEXT`, stale after a global flush until it is cleared to take a
  context, cache.rs:986-996, is skipped), so no entry leads into a dead block.
- Appended words are written by the resident for each placed copy, in `copy`, `copy_pooled` and `share` (cache.rs:1590, 1700,
  1288), never copied from another block's bytes. The stop rule for appended words (4.5) also stands before the
  dead-block filter, as the slot rule does.
- Only absolute code gets pads in this plan. A return to a caller in shared code finds R's ordinary entry: it pops, and is
  predicted wrong, as today.

A predicted return runs the ret's pop and checks (unchanged), `b slot`, the probe, `ret x1` (predicted), the pad's `b`, R's
entry.

### 4.3 Containment

EL0 gains no writable and no new readable memory: the tables are the existing ones, and a pad's address is a code-area
address of the vCPU's own blocks of the same class, as the entries user code can already read there are (layout page section
2). There is no stack of host addresses in guest-writable memory, which the guest-state page rule forbids ([boot features
plan](boot-features-plan.md), G1: "no emulator-trusted value lives there"). By the invariant, the slot's compare of the entry's
RIP with x0, and the per-context tables, a return reaches R's admitted code in its own context through its full entry, or the
resident with x0 = R. The `link-check` build scans the tables (skipping `NO_CONTEXT` ones): every target is its owner's full
entry, or a pad of a live block whose end is the entry's RIP and whose word is `b <full_entry(owner)>`. The existing table checks (`tables-*` in
`frame-randomization`, `check_l1_lists`, cache.rs:1079) assume target = owner's full entry and are extended the same way.

Against [INV-03-01](../research/investigations/INV-03-01.md) (lines 5671-5681): an entry holds the guest return identity and a
target that is a resident trampoline of one direct branch to an external guarded entry, filled and emptied by the resident.
Owner decision (November 23, 2026): the pad counts as that rule's "external guarded entry", with the invariant "a
return-table entry (R, pad) exists only while the pad word is `b full_entry(owner)`" written into the rule; INV-03-01 records
it after the rule. No other CPU loads these targets (per-vCPU code), so the rule's in-flight census is empty.

### 4.4 Stale state and unpaired returns

| Event | What happens |
| --- | --- |
| Flush | everything starts empty |
| T dies (the call's target) | the call's link word is restored; pair+0 is unreachable; the pad is untouched |
| R's block dies or stops being current; CR3 load; table handover | its entry is emptied by owner; the pad word is stale and unreachable |
| The caller dies | the entries naming its pad are emptied |
| CPL change, another context | another table; a stale RAS entry is a mispredict |

`longjmp`, C++ exceptions, signal returns, retpolines and return thunks, coroutines and `switch_to` make returns that do not
match the last call. Each is correct (the table is indexed by the RIP popped); the RAS mispredicts and stays offset until it
drains, as on a native core. A deep resident or kernel path can overwrite the oldest guest entries; whether HVF or the core
clears the RAS at an exception or `eret` is not known. The in-VM check (section 6) measures it.

### 4.5 Exactness, 32-bit code, layout, stops, the sampler

- **Exactness.** No new guest-visible effect: a call's push and its fault happen in the body as now; `bl` runs at the exit and
  writes only x30, no guest state; the ret's pop and branch-target check come before the slot.
- **32-bit code.** The same; R is the zero-extended 32-bit address; `ret imm16` is a return; get-PC thunks pair.
- **Hot/cold, extended blocks.** A call ends a unit, so a block has one call pair, after its whole code in the cold side.
  Later: the pair in the hot stream, and a target's hot copy following the caller's so that `bl` falls into it with no pad.
- **Stops.** Appended words are block boundaries: an interrupt there stops at once (pad: next RIP x0 = R; pair+0: the call's
  target from the link point's record, x0 not being set there), checked before the dead-block filter (execution.rs:2601).
  `arm` patches the link word and the computed exit as now.
- **The sampler.** The return form is in the slot area (`DISPATCH`, with the call test); appended words use the indirect
  plan's `TAIL` place with the roles pair and pad.

### 4.6 PAC and a software return stack

A stack of (guest return address, host continuation) written by each call into EL0-writable memory would need the
continuation authenticated (a guest store reaches any EL0-writable page since E3), as the [phase 2 backlog](perf-phase2-plan.md)
(lines 379-386) proposed with PAC; it costs a store and a sign per call and a register or memory for the stack pointer, and a
`br` at the return site still mispredicts for many callers unless `bl`/`ret` are used. Whether HVF exposes PAuth is not known
(natemu reads `ID_AA64ISAR1_EL1`, admission.rs:22-28, but no PAuth field). This design needs neither; the backlog item is
dropped if the kill-test passes.

### 4.7 Admission and fexc

fexc does not change. Admission returns hints only (4.1). Pads, call pairs, the stubs and the return form are resident code,
as the slots are; the code-shape ban on `bl`/`ret` (structure.rs:39) stays a rule about fexc's output.

### 4.8 What is left for later

Pads in shared code (a pad names a template's full entry, only for R on the caller's own frames, hints per instance), pairs in
the hot stream, the fall-through layout.

### 4.9 Why it should not lose time

| What made pairing lose | Here |
| --- | --- |
| A store and pointer update per call | none |
| Unbalanced pushes and pops | balanced on every path (4.1); the rest counted and priced |
| A `ret` mispredicted where today's `br` was predicted | only when calls do not pair; a single-caller return is predicted by both and pays the next row |
| Extra taken branches | the pad's `b` on return; the link word's `b` to pair+0 on a call |
| Extra words | 2 per call block, cold |
| A dearer miss | the miss pops through a stub: one more `ret` than today's miss |

The loss case is a return the current `br` already predicts (one caller, a short pattern): bounded by the kill-test's K = 1
threshold.

## 5. Smallest useful version first

On top of the indirect plan's step 1: call pairs for direct calls in absolute code and for local calls in shared code, the push
and pop stubs, `call $+5` as `b`, the return form for every return, pads and hints in absolute code, the stop rule, the dead
caller's entries emptied, the `link-check` table scan, the counters (pads filled and hit, unbalanced paths, refills after a
table handover). It covers the kernel and every process whose code has one address: `scimark32-sse2`, `scimark32`, `sqlite`,
`zstd`, `hackbench`'s kernel side. Multi-process user code (`build`, `startup32`, configure and make) stays balanced but
unpredicted until pads in shared code.

## 6. Kill-test

**Host part, mechanism only (under an hour, before any natemu change).** The indirect plan's `host-dispatch-micro` case
(`tests/bench`, M2 Max, labelled host-native, never on the benchmarks page), return variants: V0 (today: `b` calls, the slot's
`br x1`), A (pairs, `ret x1` to the pad), A0 (the return site's own `bl lookup; br x1`). Callers K = 1, 2, 4, 16, 64, visited in
a short period (64) and a long one (64 Ki or more); a variant with 1 call in 8 not pushing, to price an unbalanced path; a
recursion sweep for the RAS depth; a system call at the bottom of a chain of depth D = 4 to 32, measuring the unwind's cost
against D. It answers: does `ret x1` predict from the RAS, how deep is it, what does K = 1 lose, what does an unbalanced call
cost, does a kernel round trip clear it. Stop if `ret x1` is not predicted from the RAS, or if K = 1 loses more than 1 ns per
pair; choose A0 if within 0.5 ns of A at K = 4 and 16 in the long period.

**In-VM part, the decision (after the smallest version).** Only the VM answers what EL0 to the resident at EL1 and back, and
HVF's exits, do to the RAS. A new micro body (`tools/bench/bodies/callchain.asm`, run by `micro-natemu`): a call chain of depth
D = 4, 16, 32 with four or more callers per level, visited in a long-period order (64 Ki or more), whose bottom makes a trip,
and the same chain without the trip; a single-caller variant of the chain; and the existing `call` body (K = 1). Two trips:
`cpuid`, a system cut served in the resident (EL0 to EL1 and `eret`, no HVF exit), and a port access, which the host serves
(every IN/OUT leaves the VM: an HVF exit and back; [sampler plan](el0-sampler-plan.md) section 3), on a port the driver
program's machine accepts. Base and fix, one round on the M2 Max. Stop if the multi-caller chain without a trip saves less than
1.5 ns per return, or if the single-caller chain or `call` loses more than 1 ns per pair. If a chain with a trip saves nothing at
D = 16, record which trip clears the RAS and expect no gain on `hackbench` from it. If no port fits the driver program, the
HVF-exit question is left to the A/B (`guest-hackbench`).

## 7. Tests

Existing families: `faults` (every stack and call/ret form at block sizes default and 1), `system` (`frame-randomization` and
its `tables-*` checks, `run_k1.py`, the relink mutations), `smp` (`privilege`, `coherence`, `contain`), `programs`,
`correctness`, `boot`, `io`, the Rust units, the `link-check` build.

New cases, each shown to fail when it should (a mutation in `run_relink_mutations.py` or a negative unit test):

| Case | Shows | Fails under the mutation |
| --- | --- | --- |
| `system` `ret-pairs`, 64-bit and i386 builds: calls from K sites, recursion past 64, `longjmp`, a signal handler's return, a rewritten return address, `call $+5; pop`, `ret imm16` | every return lands right; counters show pads filled and hit and no unbalanced path but the listed ones | pad word written after the entry; a pad naming a block of another context; `call $+5` as `bl`; a resident continuation entering a call's target directly; `relink` writing a plain `b` (caught by the push count after a CR3 load) |
| `system` `ret-death`: callee and return pages rewritten (SMC) and remapped (CR3) between call and return, CR3 loads that suspend and relink linked calls, the caller killed while its pad is filled, a global flush, at 1 and 4 vCPUs | no entry names a dead block or a stale pad (`link-check` scan); no `LINK_CHECK` or `BAD_LINK` halt | the dead caller's entries not emptied; `cut_one` skipping pad entries; `unlink_word` comparing `link_at` with the entry |
| `system` `ret-stop`: interrupts landing on a pad, on pair+0, on the stubs, between a site's lookup and its `blr`, and on a dead caller's pad; a shadow-setting instruction before a call | the stop is taken at R or T, no `BAD_STOP`; counters show the push stub used and the counted cases | the appended-word rule after the dead filter; the push stub used with `shadow_entry` set |
| Rust unit: the invariant over a model of fills, deaths and handovers | each part of 4.2 | each code path broken in turn |

While iterating: `tools/natemu-test run tests/system -k 'ret- or relink'`, `tests/faults -k 'call or ret'`, the Rust units; the
affected tier (`tools/natemu-test run`) at the end, since every exit and the tables change. One gate, over both plans.

## 8. Runs (fixed budget)

| Run | What | Budget |
| --- | --- | --- |
| Host kill-test | `host-dispatch-micro`, return variants (in the indirect plan's run) | 10 min exclusive |
| In-VM check | `micro-natemu` with `callchain` and `call`, base and fix | 20 min |
| Iteration | narrow runs; the affected tier once | no heavy runs |
| A/B | M2 Max, base (indirect step 1) and fix, alternating, three rounds (`TESTKIT_BENCH_AT`): `guest-scimark32-sse2`, `guest-scimark32`, `guest-sqlite`, `guest-hackbench`, `micro-natemu`; noise is the spread across rounds; flush counts, reclaims and table refills reported | about 1.5 h |
| Profile | `guest-scimark32-sse2-profile` with the fix: the return share of slot samples should fall | 10 min |
| Gate | once, over both plans, at the end | one gate |
| Benchmarks page | only if a number moves beyond its noise: those cases on the M4, alternating, three rounds | owner schedules the M4 |

## 9. Estimate (agent hours, low confidence)

On top of the indirect plan's step 1: call pairs and stubs 3, return form 1, pads, hints and the invariant's emptying 3, stops
1, `link-check` scan and table checks 1.5, the call link's record through pair+0 (`unlink_word`, `relink`, `suspend`) 2.5, the
`callchain` body and its variants 1.5, tests and mutations 3, review fixes 1.5: about 18 hours.
Pads in shared code: 5-6 more.

## 10. Risks

- The RAS does not survive the resident's trips or HVF's exits: kernel- and syscall-heavy code gains little (the in-VM check).
- Paths left unbalanced (single-step, emulated calls) are more frequent than expected; the counters and the A/B show it.
- Single-caller returns get slightly slower (bounded by the K = 1 thresholds).
- The four tables are cleared at a handover between contexts: pads are refilled from hints after each; the A/B counts refills.
- Appended words grow the code area, the cache's limit (abi execution.rs:335-336): the A/B reports flushes.
- The new resident paths (stubs, the stop rule, hints) sit beside the linker's subtlest code.
- The earlier, unrecorded experiment may have lost for a reason not in section 4.9.

## 11. Decision: the sampler number

Build it when the slot's samples whose target passes the call test (`DISPATCH`, call test yes) plus the first-word samples of
blocks at return addresses are 3% or more of EL0 samples on a benchmark the smallest version covers: `scimark32-sse2`,
`scimark32` or `hackbench`'s kernel side from the existing profile cases (`guest-scimark32-sse2-profile`,
`guest-scimark32-profile`, `guest-hackbench32-profile`), or `sqlite` once a `guest-sqlite-profile` case is added (one case in
`tests/bench/family.toml`); and the host kill-test passes. `build`'s share is shared code and does not count for this version.
Under 3%: recorded and left ([sampler plan](el0-sampler-plan.md) section 7, last rule).

## 12. Verified, unverified, decided

Read in the code: everything cited above with file and line. Not verified: the RAS's depth on Apple cores; whether `ret x1`
predicts from it; whether an exception, `eret` or an HVF exit clears it; whether HVF exposes PAuth; that no path but a flush
reuses a dead block's bytes (placement reads so); the branch costs in 4.9 (estimates).

Defaults taken from the review: the resident appends words instead of a fexc change; a single-caller loss is accepted within
the K = 1 thresholds; the slot area grows to 32 KiB (the indirect plan); the PAC backlog item is dropped if the kill-test
passes; `host-dispatch-micro` is a bench case. Owner decision (November 23): the pad is INV-03-01's "external guarded entry"
with the invariant of 4.2 written into that rule. No question is left open.

## 13. Review

Independent design review (Fable, November 23) of revision 1: rework. Taken in: the return stack balanced on every path,
including indirect calls (`blr`, so the indirect plan's lookup form comes first), the first execution and other resident
continuations of a call (push stub), a return-form miss (a `ret x1` stub), with the unbalanced rest counted (4.1); the decision
read on benchmarks the built version covers, not `build` (11); the kill-test split into a host part that gives mechanism facts
(caller periods short and long, an unbalanced variant, a system call at the bottom of a chain of depth D) and an in-VM part with
a body that trips to the resident at depth, where the stop threshold now is (6); pads without link records, under one invariant
emptied by owner, with the full entry always (4.2); a dead caller's entries emptied and the stop rule before the dead filter
(4.2, 4.5); the four tables' handover refills counted, the existing table checks extended, `ret-32` folded into `ret-pairs`,
the census addition dropped, the order (indirect step 1 first) and the estimate.

Re-review (Fable, November 23) of revision 2: accept with fixes. Taken in: the call's link record through pair+0 (`unlink_word`,
`relink`, `suspend`; only the `link_at` word patched; a mutation for a relink that writes a plain `b`) (4.2); the in-VM chain
with four or more callers per level in a long period under the 1.5 ns bar, the single-caller chain under the 1 ns bar, `cpuid`
said to be a resident trip and a host-served port trip added for HVF exits (6); the push stub's own return mispredicted once
with stub+4 a safe target, the stubs in the slot area under its stop rule, stops before a push continuing through the stub, the
stubs unused while `shadow_entry` is set (4.1); `NO_CONTEXT` tables skipped; appended words written per copy (4.2); the
estimate.

## 14. Findings: the smallest version built (November 24, 2026)

**In-VM kill-test** (bench cases `micro-callchain` and `micro-callchain-base`, `tools/bench/bodies/callchain.asm`, M2 Max,
one round as section 8 says; base ad452224, the indirect plan's step 1; the fix the build below at 4b0e81c9, before the rebase onto
bd97a67d and the hint hash fix, neither of which changes these paths: the chains' return addresses never collided in the hint table). Three repetitions per
case, the slope between N and 2N iterations; ns per iteration median (range), and ns saved per return (a call chain of depth
D makes D + 1 calls and returns per iteration):

| Case | base | fix | saved per return | bar |
| --- | ---: | ---: | ---: | --- |
| `chain4-d4` (4 callers per level, xorshift order, depth 4) | 29.25 (29.20-29.51) | 25.13 (24.64-26.98) | 0.82 | 1.5: **below** |
| `chain4-d16` | 175.26 (175.08-175.33) | 96.80 (93.51-102.36) | 4.62 | 1.5: passes |
| `chain4-d32` | 365.38 (365.07-365.70) | 189.68 (189.47-205.19) | 5.32 | 1.5: passes |
| `chain1-d16` (one caller per level) | 27.30 (27.28-27.60) | 26.00 (25.89-27.84) | +0.08 | loss under 1: passes |
| `call` (K = 1) | 1.86 (1.80-1.87) | 2.40 (2.39-2.49) | -0.54 per pair | loss under 1: passes |
| `chain4-d16-cpuid` (CPUID at the bottom: the resident) | 523.81 (523.62-533.36) | 466.81 (466.09-484.43) | 3.35 | |
| `chain4-d16-port` (IN from port 0x80: an HVF exit) | 1807.96 (1773.16-1810.62) | 1810.39 (1783.86-1818.90) | -0.14 | |

Read: `ret x1` predicts from the return stack in the VM: at depth 16 and 32 a return saves 4.6-5.3 ns (the 1.5 ns bar three
times over). At depth 4 it saves 0.82: with five returns per iteration and eight random bits, the shared `br` predicts part of
them from its history. The plan's bar does not say whether it holds at every depth; the owner decides (below). A single caller
loses nothing (+0.08), the `call` body 0.54 ns per pair (bar 1). The resident's trip (CPUID, EL0 to EL1 and back) keeps most
of the stack (3.35 of 4.62 at depth 16); an HVF exit clears it (nothing saved): `hackbench`'s kernel side gains from it only
between host exits.

**Built** (on bd97a67d, step 1 with its review fix: every link to appended words is a real `b`, no gap after the code):

- Admission's hint `FORM_RETURN` (C3, C2) gives a return its slot's return form (`ret x1`); with no slot, slot 0's return form
  (`adr x1, miss; ret x1`, `RETURN_MISS`). `Block::exit_branch` says where a computed exit goes; `append` writes it per copy, the
  reuse check compares it.
- Call pairs (`Block::pair`, 8 bytes right after the appended entries; `Block::tail_extent` = entries + pair) for every block whose hint marks call points: a direct call's
  link word is `b pair+0` (always a `b`, never `link_word`'s `nop`), pair+0 `bl` to the target's link entry, written first.
  `link`'s check, `unlink_word` and `relink` go through it (`point_links_to`, `calls_to`); a relinked call still pushes. Local
  links of shared code use the pair too.
- A checked call from absolute code goes to its slot's call form (`SLOT_CALL`, 192: `bl` lookup, `blr x1`, `b` miss). An
  indirect call placed with no slot calls slot 0's lookup form (the miss's words), so it pushes on that path too.
- Stubs in slot 0 (`PUSH_STUB` `blr x1; b miss`, `POP_STUB` `ret x1`); `State::ras`/`ras_rip` carry the continuation to `enter`,
  which takes it only with no shadow, at the block's ordinary entry and into the exit's own target (else counted unbalanced).
  Set by the linker (a call's first execution), `stopped` (a call's or a return's exit word, `exit_ras`), and the stop rules:
  slot-area forms (return form and pop stub: pop; call form and push stub: push; lookup form: counted), call tails before their
  `blr`, pair+0 (the RIP from the call point).
- Pads (absolute code only: a pair's second word, an absolute indirect call tail's third) without link records. Hints
  (`RET_HINTS`, 4 Ki entries by a multiplicative hash of R, after the data tables, not mapped at EL0) are written at placement
  (the first build indexed by R's low 12 bits: return addresses at page starts collided, `ret-death` showed it); `fill_l1` and a call's
  link (`pad_at_link`) name a pad only when the hinted caller is alive, absolute, of the owner's and the table's context, ends at
  R and has that pad; `set_pad` empties any entry naming the pad with another owner, then writes and synchronizes the word.
  A dying caller's entries are emptied (`empty_pad_entries`, `NO_CONTEXT` tables skipped).
- Audits: the host's `check_links` (every call point linked through its pair, pairs, pads, return exits, the call form); the
  `link-check` resident's `check_tails` and a new `check_l1_targets` at each TLB flush (every entry: its owner's full entry, or the
  pad of a live block ending at the entry's RIP, of its owner's context, whose word is `b` to that full entry). The
  `tables-*` checks of `frame-randomization` compare RIP words, not targets: unchanged.
- Stop rules: call pairs and call tails on `block_at`'s miss path, before the dead-block filter (step 1's rule, moved there by
  its review fix).
- Counters `pic::count` 40-51, sampler role `TAIL_PAIR`, test entries `NATEMU_PIC_TEST=ras0..ras4` (14-18).

Deviations: no `ret-pairs` i386 build (32-bit code takes the same paths: `compat32` ran); no signal handler's return in
`ret-pairs` (K1 bodies run at CPL 0; the longjmp and the rewritten return address are its unmatched returns). A call to a
SYSCALL target served by `sys_fast` is not continued through the push stub and not counted. `sample_report.py` does not split
pair samples out yet.

**Tests** (this worktree, M2 Max, narrow runs only, no gate):

| Case | Shows |
| --- | --- |
| `system` `ret-pairs` (K1 `ret-pairs`, `ret-death`; 1 and 4 vCPUs, block sizes default and 1) | `ret-pairs`: four call sites of a leaf, recursion 80 deep, `call $+5; pop`, `ret 8`, a rewritten return address, a longjmp across two frames, `call rax`; in table A, B, A, under a second code context (MXCSR) and the first again: exact sums; counters per CPU: 62-67 pair links, 21-24 relinks, 70 pads filled from hints, 62-67 push-stub entries. `ret-death`: the return page rewritten, the caller's page rewritten while its pad is named (12 entries emptied; 212 pads filled), the return page another frame after a CR3 load in the callee, two global flushes: exact sums. |
| `system` `ret-pairs-lc` | The same and `cr3-relink`, `relink-smc` on the `link-check` resident. |
| `system` `ret-stop` (`run_pic.py` modes `ras0`-`ras3`, `tail1`; `ret-pairs`, `ret-death`, `indirect-sites`, `miss-boundary`) | Entries through the push stub (24,876) and the pop stub (23,577), at pads (1,470 stops) and pairs (6), between a lookup and its `blr` (tail1); no `BAD_STOP`, every program's values; the shadow entries of `miss-boundary` not through a stub (counted). |
| Rust units | The forms (call form, stubs, slot 0's return form), the pair, pad and exit layout, the return hint. |

Each new check was shown to fail when it should: the edit applied, the case run through `tools/natemu-test`, reverted.

| Mutation | Red in |
| --- | --- |
| `relink` writes a plain `b` to the entry (not through the pair) | `ret-pairs`: `bad_link` (the linker's check of the relinked point), the host audit |
| The linker enters a call's target directly (no push stub) | `ret-pairs`: push-stub entries 0 |
| `call $+5` hinted as a call | Rust unit `exit_forms_from_the_last_instruction` |
| The dead caller's entries not emptied | `ret-pairs-lc`: `link_check` (`check_l1_targets`: a pad of a dead block) |
| `cut_one` skipping entries that name a pad | `ret-pairs` (`ret-death`, block size 1): the stale pad ran the dead R1 block's old bytes (after the hint fix; before it the entry never held a pad: green, which found the hash defect) |
| `unlink_word` comparing the link word with the entry | `ret-pairs`: the CR3 load's suspend fails |
| The pad word not written before the entry (for "written after": single-threaded per-vCPU code, written before the next EL0 entry, so the order alone is not observable) | `ret-pairs-lc`: `link_check` |
| The pair stop rule behind the dead-block filter | `ret-stop` `ras3`: `bad_stop` at a dead block's pair |
| The push stub used with `shadow_entry` set | `ret-stop` `ras0`: no shadow entry counted |
| A pad filled across code contexts (`pad_for`'s context check removed) | `ret-pairs-lc`: `link_check` (`check_l1_targets`) |
| A return exit written to the next slot's return form | `ret-pairs`: the host audit (a return exit to a slot not serving the block's context) |
| `empty_pad_entries` scanning only `NO_CONTEXT` tables | `ret-pairs-lc` (`ret-death`): `link_check` |
| `pad_at_link` not called | `ret-pairs` (`ret-death`): no pad named at a link (counter 43) |

The first ten mutations were run at the build before the rebase onto bd97a67d (the `cut_one` one again after it and the hint
fix); the last four after the review fixes below. The context mutation was green at first: CR4.OSFXSR, set before the
second-context phase, is itself in the code context, so the "first context again" was a third one with no blocks yet. `ret-pairs`
now sets it before its first phase.

**Review fixes** (independent review of 81e05284: accept with fixes, none blocking). Both audits require a return exit to go to
its own context's slot's return form or slot 0's. A block gets a call pair only when its hint marks exactly one link point (a
`jcc T` before `call T`, or any second point to the call's target, gets none: the call is then a plain link, unbalanced, never a
wrong target or a jump that pushes; Rust unit). A continuation through a stub is dropped, and counted unbalanced, when the
resident runs an instruction itself (`system`, `step`). New coverage: `ret32` in `compat32-resident-limits` (32-bit code: pairs
linked, pads hinted, push-stub entries); `ret-stop` mode `ras4` (every block placed as with no slot, exits armed: a return's
stop continues through the pop stub; 65,079 pop entries); `ret-death` links a caller placed again while its return address's
entry names the full entry (12 pads named at a link).

Imbalances not counted (deviations): a call from the single-step block (the linker does not treat it as a call); a call whose
target is a kept SYSCALL, SYSENTER, INT or IRET, served by `sys_fast` before the linker (no push stub); a return made by a jump
(`pop; jmp reg`, a retpoline's `ret` used as a jump), which the hints cannot see. Owed: a signal handler's return (K1 bodies run
at CPL 0; no existing CPL-3 family delivers a signal cheaply).

Narrow runs at the final code (bd97a67d plus this work and the review fixes), all passing: the Rust units (NUNIT passed); `system` NSYS checks
(`k1-c1`, `k1-c2@*`, `indirect-*`, `pic-*`, `frame-randomization*`, `compat32*`, `ret-*`); `smp` `coherence`, `contain`,
`privilege` (NSMP); `faults` `faults@*`, `faults-1@*`, `probes@*` (NFAULT). Not run: the gate, `programs`, `correctness`, `boot`,
`io`. The A/B and the profile are the owner's pooled round (section 8).

**For the owner.** (1) The kill-test at depth 4 saves 0.82 ns per return, under the 1.5 ns bar; depths 16 and 32 pass three
times over. The build went on (the plan's decision case is the deep chain); whether the bar is per depth is the owner's call.
(2) An HVF exit clears the return stack: no gain across host exits. 
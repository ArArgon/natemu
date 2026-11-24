# Returns predicted by the Arm return stack (plan)

Status: plan, November 23, 2026, on `m2-sampler` at d7a662c7. Design only: nothing is built, nothing was run for it, and
it has not been reviewed. Before anything is built it takes an independent design review (AGENTS.md), and the sampler
number in section 11 must be met. The companion plan for indirect `jmp`/`call`, [indirect-cache-plan.md](indirect-cache-plan.md),
uses the same two pieces of machinery (the slot forms of section 4.1 and the words appended to a block of section 4.2);
they are described once, here.

Why two plans and not one: the two fixes are decided by different sampler numbers, target different benchmarks and are
measured by separate A/Bs; the indirect plan's first step is a few hours and may well go first. What they share is
small and is built by whichever goes first.

## 1. Why

Every guest `ret` leaves its block through a computed exit: `mov x0, xR; b <slot>`, where the slot (one per code context)
probes the context's data table (`area::DL1`) and ends in `br x1` (`cache::write_slot`, crates/runtime/src/cache.rs:663-715,
the 13 words at 682-699). One `br` serves every return of the context, so its target is a function of the guest RIP that
the branch predictor sees through one branch address. The [layout page](translated-code-layout.md) (section 7, line 117)
puts a return at about 9 ns more than a jump-linked block. Chunk 28's microbenchmark (an experiment, before the data tables)
measured a dispatcher exit at 1-2.4 ns with a predictable target and 4.9-5.4 ns with a random one, and 77-80% of the
dispatcher's samples on the word the `br` lands on ([guest-code review](guest-code-review-20261114.md) section 3, item 1):
the cost is the misprediction, not the probe.

The census (bench cases `guest-<b>-census`, QEMU with the census plugin on the M2 Max, run 1 of each benchmark) gives the
returns: 9 M (`scimark32-sse2`) to 720 M (`startup32`) per run, about 13-100 M per second at natemu's 1-vCPU times, and a
model return stack per vCPU with user and kernel kept apart hits 99.7% or more at depth 16 on every benchmark (97% or more at
depth 8). `hackbench`'s returns are 90% kernel.

An earlier experiment that paired returns with calls lost time on 64-bit code ([task 4 findings](m2-task4-findings.md),
lines 447-453); its record was not found. Section 4.15 says why this design should not lose, and section 6 measures it
before anything else is built.

## 2. The design in one paragraph

A call is linked as `bl` instead of `b`, so the core's own return-address stack (the RAS: pushed by `bl`/`blr`, popped by
`ret`) records the host address of the word after it, a *pad*. A return goes, as now, to its context's slot, which probes the
same data table and ends in `ret x1` instead of `br x1`. For a return address R the table holds the pad of the call that
returns to R. So the `ret`'s target comes from the resident-written table, exactly as today, and the RAS only predicts it:
when the guest's calls and returns pair up, the prediction is right and the return costs the probe and two direct branches.
Nothing new is stored at a call, nothing EL0 can write is ever trusted, and when the prediction is wrong the return costs
what it costs today. The resident does it alone: no fexc change, no new admitted instruction.

## 3. What exists (read in the code)

- The code area, the data tables and the dispatcher slots are the vCPU's own: "Blocks and L1 are this vCPU's own (its code
  area is backed by its own memory), so a patch stops no other vCPU" (cache.rs:13-14; layout in
  crates/abi/src/execution.rs:250-257). No other vCPU runs or patches this code. Placement is a bump fill (`reserve`,
  cache.rs:105-112).
- Data tables (`area::DL1`, abi execution.rs:341-357): table t, entry i = (RIP, address to branch to); EL0 read-only, each
  shadow space maps only its own privilege class's two tables ([layout page](translated-code-layout.md) section 2). A table
  holds one code context (`select_l1`, cache.rs:756); a context's slot names its table (`bind_slot`, cache.rs:718). An
  entry's owner (block number + 1) is kept beside it (`l1_owner`, cache.rs:571); `fill_l1` writes `(rip,
  code_of(block).full_entry())` (cache.rs:789-834); `cut_one` empties every entry its block owns when the block dies or
  stops being current (cache.rs:3142-3168); a CR3 load empties the listed non-global entries; a table changing hands is
  cleared (`clear_l1`, cache.rs:609).
- The slot area: 128 slots of 128 bytes (`DISPATCH_BYTES` 0x4000, `DISPATCH_SLOT_BYTES` 128, abi execution.rs:282-287);
  a computed exit's `b` is relocated at placement to `dispatcher_for(context)` (cache.rs:738, `relocate` at 2124).
- Exits ([admission's structure rules](../../crates/host/src/admission/structure.rs), lines 22-30): a literal exit is a
  link point, a guest-RIP move into x0 then `svc #LINK`, entered only at its first word, which the linker rewrites to `b`
  the target's entry (`link`, cache.rs:2292). At most one computed exit per block (structure.rs:998-1013), at most
  `MAX_LINK_POINTS` = 4 link points (abi execution.rs:2522). "x30 is an ordinary allocator temporary: no `bl` is admitted"
  (structure.rs:61): x30 holds nothing at a block boundary.
- Link records keep the original word and are undone by `prune` (cache.rs:3174) when the source or target dies or the
  target stops being current, by segment (abi execution.rs:753-760); `link-check` builds verify them
  (`check_no_dead_links`, cache.rs:3100).
- Shared position-independent code ([pic32 findings](m2-pic32-findings.md), [reuse32 findings](m2-reuse32-findings.md)):
  an instance has no code of its own (`instance`, cache.rs:1407); its template's full entry, `and x0, x0, #!0xfff; str x0,
  [x28, #PAGE_BASE]`, takes x0 = the RIP (abi execution.rs:1350-1358); links out of shared code to another page are *checked*:
  the exit's move stays and its `svc` word becomes `b` to the dispatcher slot (cache.rs:2300-2312). Shared code is not split
  hot/cold.
- Interrupts: in the slot area the vCPU stops at once with the next RIP in x0 (`interrupted`, crates/runtime/src/
  execution.rs:2583); inside a block the stop is armed by patching its exits (`arm`, cache.rs:2167).
- The sampler classes a PC in the slot area as `DISPATCH` with x0 as the target and a call test on the bytes before it
  (crates/abi/src/sample.rs:156-159, 186-192, `call_test` at 371).

## 4. The design

### 4.1 The slot's forms (shared with the indirect plan)

Each bound slot gets three forms, all probing the same table with the same first words:

| Form | Ends in | Used by |
| --- | --- | --- |
| ordinary (today's) | `br x1` (both ways) | computed exits not changed, checked links not changed |
| return | `ret x1` (both ways); miss: `b DISPATCH_MISS` | returns (section 4.3) |
| lookup | `ret` with x1 = the target (miss: x1 = `CODE_VA + DISPATCH_MISS`) | the indirect plan's per-site branch |

Three forms of 13 words do not fit 128 bytes: the slot grows to 256 bytes and `DISPATCH_BYTES` to 32 KiB (the probes and
`HEAD_BYTES` move with it; they are constants with asserts). An unbound slot's return form is `b DISPATCH_MISS`, its lookup
form `adr x1, DISPATCH_MISS; ret`. `write_slot` writes all three under one synchronization, as it writes one today. None of
these words writes NZCV (the dispatcher's rule, gates.S:253-257).

### 4.2 Words appended to a block (shared with the indirect plan)

The resident appends a few words of its own after a block's placed code (in the segment's code side, cold), as it writes the
hot copy's trampolines today. They are resident code, not admitted code, and have no guest semantics: each is reached from a
known place and leads to a block entry, a slot or the resident. The record gains their extent (`tail` bytes), and
`block_at` covers them. For a return-predicting call they are two words, the *call pair*:

```
pair+0:  bl  <T's entry>         (linked), the call's literal exit word branching here
pair+4:  b   DISPATCH_MISS        (the pad, unlinked)  or  b <X's full entry> (the pad, linked)
```

Words appended per block: 2 for a call (this plan), 2-3 for a computed exit or checked link (the indirect plan).

### 4.3 Calls, returns and pads

- **Which exits.** Admission already decodes a block's ending instruction (`branch_target`, admission.rs:787-800). It
  returns two more facts with the link points: the computed exit is a near `ret` (`C3`, `C2 iw`, with prefixes), and a link
  point is a near direct call's target (`E8`) whose block end R is the return address. These are hints: section 4.4 shows
  that nothing depends on them for containment or exactness. A call to the next instruction (`call $+5; pop`, which never
  returns) is not a call here: linking it as `bl` would push an entry no `ret` pops.
- **Returns.** A ret block's computed exit is relocated to its context's return form instead of the ordinary one.
- **Calls.** When the call's link point is linked to T, the resident writes pair+0 as `bl <T's entry>` (the entry `link_entry`
  chooses) and the link word as `b pair+0`. In a split block the hot copy's trampoline to the exit is rewritten to `b
  pair+0` too, so a linked call is still one hop in the hot stream. Unlinking restores the link word (and the trampoline),
  as now; pair+0 is then unreachable.
- **Pads.** The pad is reached only through a table entry. Unlinked it is `b DISPATCH_MISS`, which with x0 = R is an
  ordinary dispatcher miss. Linked it is `b <X's full entry>`, where X is the code of R's current block in the caller's
  context (absolute code: R's block; an instance: its template, whose full entry takes x0 = R). The pad's link is a link
  record like any other (source the caller, target X, segment by the usual rules), so `prune` restores it to `b
  DISPATCH_MISS` when X dies or stops being current, when the caller dies, at CR3 loads for links from another page, and at a
  flush.
- **Filling.** A table entry `(R, pad)` is written only while the pad is linked to the full entry of R's current block in
  that table's context, with R's block as the entry's owner (so `cut_one` empties it whenever R's block dies or stops being
  current). The resident writes it (a) when it links a call and R's block is current, linking the pad first, and (b) on a
  dispatcher miss for R, from a small resident-private *pad hint* table (direct-mapped by R, 4 Ki entries of (caller block,
  pad offset, context)), which `fill_l1` also consults so that an ordinary fill for R does not replace a valid pad. A hint
  is checked against the records before use (caller alive, its block end R, same context); a stale hint costs nothing but
  the miss it was consulted on.
- **Shared code (second step, section 5).** In the smallest version a call in shared code keeps its checked link and pads
  are made only for absolute code. The second step links a checked call's `svc` word as `b pair'` with pair' = `bl <lookup
  form>; blr x1; pad` (the indirect plan's form), makes hints per instance (`instance` knows R_i = its RIP + guest bytes),
  and links a shared pad only to a template on the caller's own frames (the local-link rule, cache.rs:2318-2321), whose code
  is right for every instance. For any other R the instance's entry stays the ordinary one: correct, unpredicted.

A predicted return then runs: the ret's pop and checks (unchanged), `b slot`, the probe (8 words, two loads), `ret x1`
(predicted), the pad's `b` (direct), X's entry (in shared code its two words). A call runs one more taken branch than now
when the hot trampoline cannot be rewritten (an unsplit block).

### 4.4 Containment: nothing EL0 can write is trusted

EL0 gains no writable memory and no readable memory: the tables are the existing ones, and a pad's address is a code-area
address of the vCPU's own blocks of the same privilege class, like the block entries user code can already read there
(layout page section 2). So guest code can see pads but forge nothing: there is no stack of host addresses in guest-writable
memory, which is what the guest-state page rule forbids ([boot features plan](boot-features-plan.md), the G1 decision: "no
emulator-trusted value lives there").

The invariants, each kept by the resident and checked by `link-check` builds and the cases of section 7:

1. Every table entry's target is a block's full entry (as now) or a pad.
2. An entry `(R, p)` with p a pad exists only while p is linked to the full entry of R's current block's code in the
   table's context, and is owned by that block (emptied with it by `cut_one`, by a CR3 load if it is not global, by
   `clear_l1`, by a flush).
3. A pad is either `b DISPATCH_MISS` or `b <full entry>`; it is undone only by `prune`, which leaves `b DISPATCH_MISS`.
4. A pad is reached only through a table entry (nothing in a block branches to the appended words but its own link word,
   and that one to pair+0 only). Speculation along a stale RAS entry is squashed; it has no architectural effect.
5. The slot compares the entry's RIP with x0 before branching, so at a pad x0 = R exactly.

So a return reaches only R's admitted code in its own context, through its full entry, or the resident with x0 = R. This is
the shape [INV-03-01](../research/investigations/INV-03-01.md) (lines 5671-5681) permits for a return-prediction cache: an
entry with the guest return identity and a target that leads to a guarded external entry, filled and emptied by the
resident; no unchecked native continuation. The pad is a resident trampoline inside the caller's code range, not a block
entry; the review should confirm that this reading of "external guarded entry" is acceptable. Other CPUs never load these
targets (the code is per vCPU), so INV-03-01's in-flight census is empty.

A wrong hint (a `ret` classed as a jump, a jump classed as a call) changes only which slot form or link form is used. Each
form's target is the table's or the link's, so a wrong hint costs time, never correctness.

### 4.5 Stale entries

| Event | What happens |
| --- | --- |
| Flush | code area, tables, link records and hints all start empty, as now |
| Unlinking a call (T dies) | the link word is restored; the pad is untouched and stays valid |
| R's block dies or stops being current | `cut_one` empties `(R, pad)` (owner); `prune` restores the pad |
| The caller dies | `prune` restores the pad (its source is dead); the caller's bytes are not reused before a flush; a ret through a remaining entry reaches `b DISPATCH_MISS` and is refilled once |
| CR3 load | non-global entries emptied (listed); links from another page into non-global targets pruned, as now |
| CPL change, other code context | another table and slot; the RAS is not separated by privilege, so a stale prediction is a mispredict, not an error |
| A table given to another context | `clear_l1` before the slot is rebound, as now |

### 4.6 Unwinding and other unpaired returns

`longjmp`, C++ exceptions, signal handlers returning through `__restore_rt`, retpolines and return thunks that rewrite the
return address, coroutines and the kernel's `switch_to` all make returns that do not match the last call. The table is
indexed by the RIP actually popped, so each is correct; the RAS mispredicts, as a native core does on the same code, and may
stay offset until it drains. The RAS is one per core, shared with the resident's own Rust calls and with kernel code: a deep
resident or kernel path can overwrite the oldest guest entries. Its depth, and whether HVF or the core clears it at an
exception or `eret`, are not known (unverified); the kill-test measures both on the host. The census's split model (user
and kernel apart, 99.7% at depth 16) is therefore an upper bound.

### 4.7 Exactness

No new guest-visible effect: the call's push, its fault and the guest's stack write order are the block's, unchanged; the
`bl` runs after the body, at the exit, and writes only x30, which is no guest state. The `ret`'s pop and branch-target check
are unchanged and come before the slot. A fault, an interrupt or a stop anywhere sees the same guest state as now.

### 4.8 32-bit code

The same. R is the 32-bit return address zero-extended, as the table already keys 32-bit RIPs; `ret imm16` is a return;
`call $+5` (older 32-bit PIC) is excluded (section 4.3); the get-PC thunks pair normally. The segment slots of the first user
table (`SEGMENT_SLOTS`, abi execution.rs:303-306) always target the miss and are untouched.

### 4.9 Hot/cold layout and extended blocks

A call ends a unit, so a block has at most one call pair, after its whole code in the cold side; the hot copy's trampoline to
the call exit is rewritten to the pair when the call is linked (section 4.3). The pad is in cold code: one cold line per
return while it is hot in the cache. Later, the pair can move into the hot stream (it is resident-written, like the
trampolines), and when a target's hot copy follows the caller's (the layout already allows it, abi execution.rs:1361-1366),
`bl` falls straight into R's entry and needs no pad. Extended blocks change nothing: their conditional exits come before the
call.

### 4.10 Stops, interrupts, single-step

Appended words are block boundaries: an interrupt with the PC on one stops at once, as in the slot area (execution.rs:2583):
at a pad with the next RIP x0 (= R), at pair+0 with the call's target T from the link point's record (x0 is not set there,
the move was skipped). `arm` patches the link word and the computed exit as now, which is enough: the pair is reached only
from the link word, the pad only from a slot. The single-step slot's block keeps today's exits.

### 4.11 The sampler

The return form lies in the slot area, so its samples stay `DISPATCH` with the call test (sample.rs:405-440); the report
splits the slot's offset into its three forms. Appended words get a new place, `TAIL` (8; `place` has 4 bits), with the
word's role (pair, pad) and the block's fields: today they would land as `BLOCK` offsets past the object's code, which the
report cannot map. The [sampler plan](el0-sampler-plan.md) section 7's return share is then read with the new place.

### 4.12 A software return stack, and PAC

The other shape, a per-vCPU stack of (guest return address, host continuation) written by each call into EL0-writable
memory, needs the continuation authenticated, because a guest store reaches any EL0-writable page (no address guard since
E3, [boot features plan](boot-features-plan.md)). The [phase 2 backlog](perf-phase2-plan.md) (lines 379-386) proposed PAC for
that: sign the continuation at the call with a key the resident sets, authenticate at the return. It costs a store and a
sign at every call, a stack pointer kept in a register FEX would have to give up, or in memory (a load and a store more per call), and a
`br` from the return's own site, which is still mispredicted for a function with many callers unless `bl`/`ret` are used as
here. Whether HVF exposes PAuth to the guest and lets EL1 set `SCTLR_EL1.EnIA` and the keys is not known: natemu reads
`ID_AA64ISAR1_EL1` through HVF (admission.rs:22-28) but checks no PAuth field (unverified). This design needs neither.
FEX upstream's own call-return stack keeps unchecked host addresses on such a stack; INV-03-01 refused importing it.

### 4.13 Admission and fexc

fexc does not change. Admission gains no rule: it returns the two hints of section 4.3 beside the link points and the
dispatch branch, and they bear on nothing it proves. The appended words, the slot forms, `bl` and `ret` are resident code,
as the slots and trampolines are; the code-shape check's ban on `bl`/`br`/`ret` (structure.rs:39) stays a rule about fexc's
output. The alternative, fexc emitting a placeholder word after each call exit and marking returns, would add admission
rules (the placeholder exactly there, reached by nothing, never executed as fexc code) and change every answer and the
persistent cache's key; it buys nothing the resident cannot do.

### 4.14 Why it should not lose time

What can make return pairing slower than today, and what this design does about each:

| Cost | Here |
| --- | --- |
| A store and pointer update at every call (a software stack) | none |
| A mispredicted `ret` where today's `br` was predicted | the `ret` is predicted when calls pair; when they do not, it mispredicts where the `br` mostly did too; a function with one caller is predicted by both, and then this design pays the next row |
| Extra taken branches | the pad's `b` on return; on a call, none in split blocks, one in unsplit ones (about 0.25-0.5 ns each, estimate) |
| Extra words in every block | 2 per call block, cold |
| A miss dearer than today | a miss is today's miss |
| Invalidation work at CR3 loads or context changes | none beyond today's table emptying |

The loss case is code whose returns the current `br` already predicts (one caller, or a short repeating pattern its history
learns): the extra branch then costs a little. The micro body `call` (tools/bench/bodies/call.asm, one call site) is that
case and is expected to slow slightly. The kill-test measures it.

## 5. Smallest useful version first

Absolute code only, which covers the kernel (global, never shared), and every process whose code has no second address:
`scimark32-sse2`, `sqlite`, `zstd`, `luajit`, `hackbench`'s kernel side. In it: the return form (4.1), the call pair and pads
with their link records (4.2, 4.3), the pad hints, the stop rule at appended words (4.10), the sampler place (4.11), and the
admission hints. Left for later: shared code (the second step of 4.3, which `build`, `startup32` and the configure/make steps
need, their libraries being shared across processes), pairs in the hot stream and the fall-through layout (4.9), dropping the
pair+0 hop in unsplit blocks.

If the kill-test shows the per-site branch of the indirect plan applied to returns (`bl lookup; br x1`, "A0" below) within
0.5 ns of the RAS form at the callers the census reports, A0 is built instead: it needs no pads, no links and no hints, and
comes with the indirect plan's first step.

## 6. Kill-test (before any natemu change, under an hour)

A host-native microbenchmark, as the S2g attribution ran FEX blocks on the host (tools/bench/attribution): the same timing as
in the VM for linked loops (guest-code review section 1). It is a framework case from the start, so its numbers can decide:
`host-dispatch-micro` in `tests/bench` (a generator writing the assembly, a small C driver timing with
`mach_absolute_time`, built with the Xcode command-line tools the README requires; exclusive; five rounds, variants
alternating; the result is ns per call/return pair with the spread across rounds). Shared with the indirect plan's
kill-test.

Variants, each with a table laid out as `DL1` (two ways, the slot's exact 13 words) and a leaf function of a few ALU words:

- V0, today: calls `b`, the leaf's return through the ordinary form (`br x1`) to the continuation.
- A: calls through the pair (`b pair; bl leaf; pad: b cont`), return form `ret x1` to the pad.
- A0: return through `bl lookup; br x1` at the leaf's return site.
- Callers: K = 1, 2, 4, 16, 64 call sites, visited in a fixed pseudo-random order by an indirect jump that every variant
  pays alike (and a variant with no call measures it). A recursion sweep (depth 4 to 128) finds the RAS depth. A variant
  makes a system call (`getpid`) every 64 calls, to see whether the RAS survives a kernel round trip (only indicative: an
  HVF guest's `eret` from EL1 is another path).

Kill A if at K = 4 it saves less than 1.5 ns per pair over V0, or if at K = 1 it loses more than 1 ns. Prefer A0 if within
0.5 ns of A at K = 4 and 16. After the smallest version is built, a second early check in the VM: `micro-natemu` (its `call`
body) and `guest-scimark32-sse2` (Monte Carlo is calls and returns, task 4 findings line 447), one round each, base against
the fix; stop and look again if Monte Carlo gains less than 10%.

## 7. Tests

Existing families that cover it: `faults` (every stack and call/ret form, at block sizes default and 1: exactness of calls and
returns), `system` (`frame-randomization` and its `tables-*` checks of what EL0 reads in the tables; the K1 and relink cases
of `run_k1.py` and the relink mutations, `run_relink_mutations.py`, for links and their records), `smp` (`privilege`,
`coherence`, `contain`: CPL switches, cross-vCPU invalidation, forged branches), `programs`, `correctness`, `boot`, `io`
(the io5 boots), the Rust units (`unit` family: abi, runtime, host), and the `link-check` resident build.

New cases, each shown to fail when it should (a mutation in `run_relink_mutations.py` or a negative unit test):

| Case | Shows | Fails under the mutation |
| --- | --- | --- |
| `system` `ret-pairs`: a guest program with calls from K sites, recursion past 64, `longjmp`, a signal handler's return, a return address rewritten on the stack, `call $+5; pop` | every return lands right; the counters show pads filled and hit | a pad filled while unlinked; a pad linked to a block of another context; `call $+5` linked as `bl` |
| `system` `ret-death`: the callee's and the return's pages rewritten (SMC) and remapped (CR3) between call and return, at 1 and 4 vCPUs | the return re-translates; no entry names a dead block | `cut_one` not emptying pad entries; `prune` skipping pad records (`link-check` halts) |
| `system` `ret-stop`: interrupts timed to land on a pad and on pair+0 (the timer at fine steps, as the stop cases do) | the stop is taken at R and at T | the stop rule at appended words removed |
| `system` `ret-32`: the same program built for i386 | 32-bit returns and `ret imm16` | (as `ret-pairs`) |
| Rust unit: table entry invariants (4.4, 1-5) over a model of fills and deaths | each invariant | each invariant's code path broken in turn |
| Rust unit: the sampler's `TAIL` place and the report's mapping | appended words are placed and mapped | the place dropped |

While iterating: `tools/natemu-test run tests/system -k 'ret- or relink'`, `tests/faults -k 'call or ret'`, the Rust units.
At the end of the task, since this changes the dispatcher, the tables and every block's exits: `tools/natemu-test run` (the
affected tier). The gate once, over both plans (section 8).

## 8. Runs (fixed budget)

Machine: the M2 Max (main runner) for the kill-test and the A/B; numbers there are evidence for the commit message, never the
benchmarks page.

| Run | What | Budget |
| --- | --- | --- |
| Kill-test | `host-dispatch-micro`, once | 10 min exclusive |
| Census addition | a last-target-per-site model for returns in the census plugin (what a per-site branch predicts: A0's and today's best case), with its `census-check` case; then the census cases once | 1 h of QEMU time (2 cores per case) |
| In-VM early check | `micro-natemu`, `guest-scimark32-sse2`, base and fix, one round | 20 min |
| Iteration | narrow runs (section 7); the affected tier once at the end | as needed, no heavy runs |
| A/B | base and fix on the M2 Max, alternating, three rounds (`TESTKIT_BENCH_AT`): `guest-scimark32-sse2`, `guest-hackbench`, `guest-sqlite`, `guest-startup32`, `guest-build`, `micro-natemu`; the noise is the spread across rounds | about 2 h |
| Profile | one `guest-startup32-profile` and one `guest-scimark32-sse2-profile` run with the fix: the slot's return share should fall | 20 min |
| Gate | once, over both plans, at the end | one gate |
| Benchmarks page | only if a page number moves beyond its noise: those cases on the M4 (the control-group machine), base and fix alternating, three rounds | owner schedules the M4 |

The second step (shared code) gets its own A/B of `guest-build`, `guest-startup32` and the `io5-solo32` steps' bench case,
inside the same gate.

## 9. Estimate (agent hours, low confidence)

Smallest version: kill-test case 1 (shared with the indirect plan); admission hints and their transport 2; slot forms 2;
call pair, pads, link records, hints, `fill_l1` 6; stops at appended words 1.5; sampler place and report 1.5; tests and
mutations 4; review fixes 2: about 20 hours. Shared code: 6-8 more. A0 instead of A: about 3 on top of the indirect plan's
first step.

## 10. Risks

- The RAS does not survive the resident's trips, or HVF clears it at `eret`: kernel-heavy and syscall-heavy code gains little.
  The kill-test's system-call variant and the in-VM check show it.
- Single-caller returns get slightly slower (section 4.14): a net loss on code where most returns are already predicted.
- Appended words grow the code area, which is the cache's limit ("the code area ... is now the limit", abi
  execution.rs:335-336): more flushes. 8 bytes per call block, a few percent of placed bytes (estimate); the A/B reports
  flush counts.
- Pad links add up to one link record per call block: more reclaims when the records fill (`Control::reclaims`). The A/B
  reports them.
- The stop rule at appended words, the pad hints and the record extent are new resident paths, close to the linker's
  subtlest code. The `link-check` build, the relink mutations and the new cases are the defence.
- The smallest version leaves the multi-process workloads (`build`, configure/make) mostly to the second step.
- The earlier, unrecorded experiment may have lost for a reason not listed in section 4.14.

## 11. Decision: the sampler number

Build it when, on at least one of `guest-scimark32-sse2`, `guest-startup32`, `guest-build`, `guest-sqlite`, `guest-hackbench`,
the slot's samples whose target passes the call test (`DISPATCH`, call test yes) plus the first-word samples of blocks whose
RIP passes it (the landing words) are 3% or more of EL0 samples, and the kill-test passes. Expected gain: about 0.6-0.8 of
that share (the landing words' part of the slot's cost, chunk 28), less the extra branch on returns already predicted.
Under 3% everywhere: recorded and left, as the [sampler plan](el0-sampler-plan.md)'s last rule says. (That plan's 5% rule,
line 275, was the trigger to design; this is the bar to build, set by the A/B's noise of about 1-2%.)

## 12. What is verified and what is not

Read in the code (file:line above): the slot's words and binding, the tables' layout, ownership, filling and emptying, the
per-vCPU code area, placement by bump fill, x30 as a temporary with no `bl` admitted, at most one computed exit and four
link points, shared code's full entry and checked links, the stop rule in the slot area, the sampler's places and call test,
the code synchronization's IDC/DIC handling (cache.rs:2080-2105).

Not verified: the RAS's depth on Apple cores; whether `ret x1` (not x30) is predicted from the RAS on them; whether an
exception or `eret` under HVF clears it; whether HVF exposes PAuth; that nothing reuses a dead block's bytes before a flush
(placement reads so, not checked against every path); that the resident never resumes at an appended word after a trip
other than through the stop rule; the per-branch costs in section 4.14 (estimates).

## 13. Open questions for the owner

1. The resident appending words to placed blocks (a record field and a sampler place) instead of a fexc change: agreed?
2. A small loss on single-caller returns (the `call` micro) for gains on returns with many callers: acceptable?
3. The slot area doubled to 32 KiB (three forms per context), or fewer slots (64 contexts)?
4. The pad as a resident trampoline inside the caller's code range: does it meet INV-03-01's "external guarded entry"?
5. The PAC return-address cache in the phase 2 backlog: drop it, since this design needs no authentication?
6. `host-dispatch-micro` as a bench case: a host-native number, never on the benchmarks page; acceptable as the kill-test?

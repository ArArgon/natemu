# Position-independent translations: design study

Status: design and evidence only, November 10, 2026, on `codex/virtio-io` at 6ac050e; revised the same day after two independent reviews (all blocking findings accepted; fixed in sections 3 and 4) and
the owner's inline-cache suggestion (adopted, section 3). Owner principle for the design: the common cases are fast;
rare cases (a frame at two linear pages, links from shared code into absolute or global code, odd slot states) take the
simplest correct slow path (the dispatcher, or no sharing for that block) rather than new bookkeeping; Stage 1 counts
each case so the fast paths are chosen on evidence. Nothing is built. Owner sign-off is
needed before any implementation. Inputs: the CPython profile (`natemu-raw/cpython-profile-20261110-01/`: `summary.md`;
`runs/full-off-1` and `runs/full-norand-off-1`, the `vcpu` lines of `perf-trim.log`), the capacity experiment
(`natemu-raw/capacity-20261110-01/`, runs `fullA`/`fullB`, `slice*`), [perf-plan.md](perf-plan.md) chunks 3, 4, 8 and 10
(the relocation proof, host reuse, local links, reuse inside the resident), `crates/runtime/src/cache.rs`,
`crates/runtime/src/execution.rs`, `crates/abi/src/execution.rs`, and the FEX fork's code generator (`third_party/FEX`,
`natemu` branch). Estimates are labelled as estimates and show their arithmetic.

## Recommendation in one paragraph

Build position-independent translations for user (CPL 3) code, in natemu, not in FEX: when the resident places a block
that is already a reuse template, it rewrites each guest-address constant into "load the block's page base from the
guest-state page, add an offset", in exactly the same number of instructions, and gives the block a two-instruction entry
that records the page base (the body moves 16 bytes). FEX's output and its byte-identical gate stay as they are. One
translation then serves every virtual address with the same physical page, page offset and context; each address costs
only a small instance record instead of a copy of about 425 bytes of code and metadata. Every library and relocatable
executable in the guest image fits the rewrite (all span less than 16 MiB; `cc1` and `python3.13` are fixed-address
executables that ASLR does not move). Estimated result: 70-85% of the cost of guest ASLR removed (full build about 200-225 s
against 294 s now and 186 s with ASLR off); the capacity experiment, already measured, takes 294 s to 261 s by itself and
is compatible. The design needs an address check on cross-page links (through the target's L1 entry), a per-translation instance chain for invalidation, and
an explicit trust model for the base slot (sections 3-4). Proceed in four stages, each with its own gate (section 6).

## 1. What ASLR costs now

Per vCPU, mean of the four vCPUs, 4-vCPU full build (perf build; seconds of vCPU thread CPU time). The profile is of a build
58 commits older than the current tip; the capacity experiment's baseline (`fullA`, current code, same workload) agrees:
294.5 s wall, 193.9 s CPU per vCPU, 6.34 M requests, 31.5 flushes.

| | ASLR on | ASLR off | difference |
| --- | --- | --- | --- |
| vCPU CPU time | 190.7 | 136.8 | +53.9 |
| guest code | 77.9 | 75.6 | +2.3 |
| host (translation requests) | 48.1 | 9.8 | +38.4 (of which fexc +16.1, admission +5.2, perf logging +4.6, present only in perf builds) |
| linker and patching | 27.2 | 16.7 | +10.4 |
| copying templates (`reuse`) | 3.3 | 0.0 | +3.3 |
| placement, fetch walks, lookup, entry | 10.6 | 8.0 | +2.6 |
| requests to the host (served by relocation / compiled by fexc) | 6.27 M (4.71 / 1.44) | 1.00 M (0.45 / 0.44) | |
| blocks from the host / copies | 6.13 M / 8.90 M | 0.89 M / 0.04 M | |
| capacity flushes | 28-32 | 1-2 | |
| linker entries | 42.3 M | 29.5 M | |

Mechanism: each new process maps the same library pages at new addresses. The resident serves most of them by copying a
template and rewriting its constants, but every copy is a new block with its own code and record (about 170 bytes of code
and 255 of metadata on average: about 3.6 GB written into a 128 MiB arena over the run). The arena fills about 30 times;
each flush drops the templates with the copies, so the next process's code goes to the host again, and every copy's exits
are linked again. The guest's own work barely changes.

**Why fexc compiles more with ASLR on** (1.44 M vs 0.44 M per vCPU, though its in-memory cache is shared): the host's cache
holds at most two generations of 128 MiB (`GENERATION_BYTES`, `execution.rs` host crate) and ended the run at 190 MB, near
its bound. Its miss rate is lower with ASLR on (23% of requests vs 44%), so the extra compiles come from the six-fold
request count re-asking for answers it has dropped, not from answers it cannot relocate. Consistent with that, the capacity
experiment's larger arena (half the flushes) cut fexc compiles from 1.48 M to 0.92 M. Not proven; Stage 1 counts it. A
larger host generation is a cheap separate fix (section 5).

**Calibration from the capacity experiment** (`fullA` vs `fullB`: metadata arena and index doubled, code area unchanged
at its 128 MiB cap, which then fills first): flushes 31.5 → 16.5, requests 6.34 M → 3.74 M, CPU 193.9 → 171.5 s per vCPU,
wall 294.5 → 260.7 s. So each avoided request saved about 8.6 µs of vCPU CPU and 13 µs of wall time, all flush-related
costs included (relinking, re-placing). Slice: 39.2-41.9 s → 35.7-36.0 s, ASLR-off 32.9 s.

## 2. Where translated code depends on the absolute guest address

FEX already builds every guest address from the block start: its IR names them as "entry point plus offset"
(`EntrypointOffset`, `InlineEntrypointOffset`, `GetRelocatedPC` in `OpcodeDispatcher.h`); only its back end turns them into
absolute constants. The inventory, with what each becomes:

| Dependence | Where | Today | Position-independent form |
| --- | --- | --- | --- |
| Next-instruction and branch-target addresses, call return addresses pushed, RIP-relative operands (`lea`, loads, stores) | `EntrypointOffset` → `InsertGuestRIPMove` (relocation kind 3, 1-4 move words) | `movz/movk xR, #absolute` | `ldr xR, [x28, #page_base]` then 1-3 `add/sub xR, xR, #imm` (same word count) |
| Literal exits (link points) | `BranchOps.cpp` exits: move into x0, `svc #LINK` | linker rewrites the move's first word to `b target` | same rewrite of the move into x0; link forms by class (section 3) |
| Code-tail address literal | `JIT.cpp` tail, relocation kind 1 | 8-byte slot | left as the translation's address; that nothing reads it becomes a test, and the check mode normalises it |
| Access sites, PC table, split stubs | `Site::guest_offset` etc. | offsets from the block address | unchanged; the resident adds the instance address |
| Block address in the record, lookup keys (L1 code entries, L2) | `Block::rip`, `fill_l1`, `hash` | one block per address | per-instance address in an instance record; L1 entries unchanged in form (compare the address, branch to the shared entry) |
| Fault and exit address (resident) | `b.rip + guest_offset` (`execution.rs`) | block address | the instance address, found and cross-checked as in section 4 |
| Breakpoints, stop address, hook fences, RF and single-step | `code_breakpoints`, `reuse_fenced`, step slot | compared with `b.rip`; templates refused under DR7 | per instance: the same refusals when an instance is made; the step block stays absolute |
| Canonical address checks | `relocate_code` (place, values, block end) | per copy | per instance: the translation keeps its smallest and largest offset; base plus each, and the block end, must be canonical; a crossing block's second frame must be the translation's |
| Currency (TLB epoch, GLOBAL, LARGE), linear chains for INVLPG | `Block::checked`, `flags`, `lnext` | per block | per instance |
| Frames, context, code, sites, death by a code write | `Block::source_gpa`, `DEAD` | per block | per translation (shared), plus a chain of its instances |

Instruction cost per address constant: today 2-4 words (3 for a 47-bit user address); rewritten, the same count, with one
load from the guest-state page in place of the first word. A RIP-relative access then has a load-then-add chain of about 6
cycles where the move chain had about 3 (an estimate; Stage 2 measures it). Per block entry: two more instructions (`and`
to the page, `str`). Per cross-page linked exit: the address computation now executes (2-3 instructions) where the link
skipped it, plus the address check (section 3). No register is reserved: FEX has only 7 allocatable general registers here (x18, x20-x24, x30; x25 holds the
saved flags, x26/x27 PF/AF, x28 the state), so taking one would spill; the base lives in the guest-state page, like the
frame slots natemu already added there (`KERNEL_GS_BASE`, `VERW_GDT_BASE`). Caches, L1 and code are per vCPU, and each vCPU
has its own guest-state page, so one slot per vCPU suffices. Estimated tax: 1-3% of guest time, about 1-2 s per vCPU of
the 78 s.

**Why the page base and not the block address.** A link between two blocks that start on the same linear page and frame
leaves the base unchanged, so it stays a single `b` past the target's entry instructions, as cheap as a link today.

**Reach of the same-length rewrite.** Three words reach ±16 MiB from the page base (`ldr`, `add #imm, lsl #12`, `add #imm`);
a two-word move only ±4 KiB or a page multiple below 16 MiB; a one-word `orr` or `movn` form not at all. A
position-independent image cannot refer by direct branch or RIP-relative operand outside itself, so its span bounds every
offset. Evidence (throwaway script `natemu-raw/pic-20261110-01/spans.py`, output `spans.out`, run over every x86-64 ELF in
the profile's initrd, 681 files): all 234 relocatable executables and 430 of 431 shared libraries span less than 16 MiB
(the exception is `libtsan`, 16.6 MiB, not used by the build); the three larger images (`cc1` 34.4 MiB, `lto1`,
`lto-dump`) and `python3.13` are fixed-address executables, which ASLR does not move, so they keep absolute code.
Absolute fallbacks (copied as today): one-word moves, two-word moves out of reach, kernel and other CPL-0 code, steps.

## 3. Links

A link patched into a shared translation applies to every instance of it, and an instance can be entered without the
resident seeing it: through an L1 entry filled earlier in the epoch, or through a local link from a block of the same
frame. The first draft's rule ("one linkable instance per translation") missed both and was unsound (review finding 1,
checked against `local`, `cache.rs`, and the linker, `execution.rs`): with frame F mapped at two linear pages P1 and P2 of
one address space, a local link S→T and a cross-page link from T made at P1, a jump into S at P2 reaches T at P2 by the
local link and T's P1 link then enters the wrong code if P2's neighbour maps another frame (a breakpoint's copy-on-write
page, for example).

**Rule: a cross-page link checks the target address it computes (an inline jump cache; owner's suggestion, adopted
November 10).** The exit keeps its move, so x0 holds this instance's target address; the link compares x0 with the
address the target was linked at and takes the miss path when they differ. In the alias case above, T entered at P2
computes an address other than the one its link was made for, misses, and reaches the resident, which finds the right
code. No per-frame alias tracking is needed, and the first revision's rule and its bookkeeping are deleted.

Where the compare lives. The first draft rejected a per-link compare because the exit has no room for it (the move and
the `svc` only); that still rules out an inline compare, so the check is out of line, and the cheapest out-of-line check
already exists: **the target address's L1 entry** (`movz/movk x2, #address`; `sub x2, x2, x0`; `cbnz` to the miss; `b`
to the entry). A cross-page link patches the exit's `svc` word to `b` straight into the L1 entry for the target address in
the source context's table: a computed exit minus the dispatcher's four instructions and its indirect `br`.
- Sound: L1 entries are kept exact for the current epoch already (emptied when their block stops being current at CR3
  loads, `tlb`; at INVLPG and kills, `cut_one`; refilled by lookups), so an L1 hit is as correct as a dispatcher hit, for
  any instance of the source. The one extra condition is that the table still holds the source's context: when a table
  loses its context (a global TLB flush, a reset, a table given to another context; tens a run: 43-62 global flushes per vCPU in the profile), the links into it are
  undone. Links stay within one context (`link` refuses others), and L1 tables and code are per vCPU.
- No thrash: a miss goes to the dispatcher's miss (`missed`, which completes the exit as the linker does), never back to
  the linker, so nothing re-patches; two instances alternating cost one resident lookup each when they collide in L1,
  as computed exits do today.
- Less bookkeeping than today's cross-page links: no pruning at CR3 loads, TLB flushes or INVLPG of the target (the L1
  entry tracks it); a record is kept only to restore the word (source death, table loss, flush).
- Cost per cross-page transition: today 1 instruction (the move is skipped); with the check, the move (3) plus `b`, the
  entry's move (3-4), `sub`, `cbnz`, `b`: about 10, against about 4 for an unchecked link. The owner's literal form (a
  per-link stub: `ldr x2, [lit]; cmp; b.ne; b`) is about 3 fewer, but needs 32 bytes of code area per link (the area that
  now fills first) and its own maintenance; it uses x2, not x16, which holds a guest register (`SRA`). Its 8-byte literal
  would be 8-aligned in 16-byte stubs, so alignment checking is no issue. Stage 1 counts cross-page transitions; if they
  are hot enough for 3 instructions to matter, the stub form is the fallback.

Link classes, with the word each patches:

- **Local, shared to shared only** (today's `local` rule, both translations position-independent, and the target starts
  on the source's start page; a target starting on the source's second page is not local for the base): holds for every
  instance by construction. The move's first word becomes `b` to the target's body past its two entry instructions; the
  base is unchanged. Survives CR3 loads and TLB flushes as local links do today; goes when either translation dies.
- **Checked, every other link out of a shared translation** (cross-page, or into an absolute or global target, even on
  the same page and frame): the move into x0 stays (it executes); the `svc #LINK` becomes `b` to the target address's L1
  entry. A shared source to an absolute target cannot be local (re-review blocker A, checked: `local` and the
  CR3-surviving segments in `link` and `prune`, `cache.rs`): in process A, S at P1 links locally to absolute T valid only
  at P1; after a CR3 load process B maps F at P2, and S at P2 would branch into T running with P1's addresses. Through
  the L1 entry, S at P2 computes P2's address and finds the block current there.
- **From an absolute block into a position-independent target**: the same checked form (the `svc` word, through the L1
  entry): the first-word form would leave x0 and the slot stale.
- **Absolute to absolute**: unchanged.
- **STI-first self exit**: stays unlinked; the identity test in the linker compares translation and instance, not only
  the address.

`link_point` (abi), `unlinked`, `link`, `arm`/`disarm` and the restore check (`links_restored`) learn which word a link
record names; a link record keeps its form. A precise stop arms both forms' words (a checked link's `svc` word holds
its `b`, and every path out of the block passes it).

**Invalidation reaches every instance** (review finding 2, checked: `cut_one` empties only the L1 entry at
`b.rip & L1_MASK`, and an L1 hit bypasses lookup). A translation keeps a chain of its instances; killing it (a code write,
`invalidate`; the windowless I/O code-page drain; `discard`) cuts each instance's L1 entry in every table and its L2 slot,
and prunes links into and out of it, before the page's mark is cleared. Evicting instances (instance area full) does the
same per instance; L1 owner words name instances, not translations, so `tlb`'s currency test on an owner reads the
instance.

Other events: CR3 loads and TLB flushes act on instance currency as they act on blocks now; a full flush is needed only
when translations fill the metadata arena. Instances are not made while an instruction breakpoint is armed (today's
template refusal). A revalidation whose fetch walk finds a global mapping for a shared translation's instance (lookup
copies GLOBAL and LARGE from the walk, `cache.rs`) drops that instance; the address then gets an absolute block.

## 4. Entry, the base slot, and where the work lands

**Entry contract** (review finding 4, checked: `enter` builds the frame with x0 = 0, and the host checks the rest of the
frame is zero at every stop, `entry_frame_rest_zero`). Two entry points per position-independent translation: the full
entry (`and x0, x0, #~0xfff`; `str x0, [x28, #page_base]`; body), used by L1 entries and cross-page links, which arrive
with x0 = the address; and the body entry, used by local links and by the resident's own `enter`, which writes the slot
itself and enters past the two words (the frame check stays as it is). Code that today compares the stop address with the
first word (`interrupted`: `elr == b.entry()` and `at_once(s, b.rip)`; `shadow_entry`) treats both entry words and the
body entry as "the first word", and takes the address from the right place for each: at the `and`, x0 (the full address;
the slot still holds the previous base); at the `str`, x0 (the page base) plus the block's page offset; at the body entry,
the slot plus the page offset (x0 is stale after a local link). Every address so taken is checked as below. The only
writers of the slot are the entry's own `str` and `enter` (a test).

**Body placement.** The block becomes: header word, `and`, `str`, a `nop` (full entry at offset 4), then FEX's body, which
so moves 16 bytes. A 4-byte move would misalign FEX's 8-byte literal slots, which fault under alignment checking (with
x86 AC on, generated code runs with SCTLR_EL1.A). Every branch inside a FEX block is PC-relative, so sites, link points,
the computed exit and the relocations shift together.

**Trust model for the base slot** (review finding 3, checked: the guest-state page is reachable by ordinary CPL-3 guest
accesses, a documented deviation in `abi/execution.rs`, the same class as the IF byte). The slot is guest-writable state.
Rules:
- Position-independent translations only for CPL-3 contexts and non-global mappings (so CR3 loads always end their
  instances' currency); kernel code is absolute and never reads the slot.
- The resident never trusts the slot alone. It finds the running translation T from the host PC (`block_at`), reads the
  slot, and accepts it only if it is page-aligned, canonical, has T's half, and a lookup of slot plus T's page offset in
  the current context resolves to T: T's instance there, or one made on the spot after the usual fetch walk confirms the
  page maps T's frames (the ordinary miss path, no new bookkeeping). Requiring an existing instance would reject
  legitimate runs (re-review blocker B: a local link enters T where T has no instance yet; checked against the linker's
  source test, `execution.rs`); the lookup makes that instance, so T's exits link afterwards. The linker also requires
  x0 minus the slot to equal a link point's offset.
- Stage 2 (one block per address) does not read the slot at all: the resident uses the block's own address and counts a
  slot that disagrees (a debug count, never a halt).
- A slot that fails is the guest's own corruption of its own state: it never halts the VM (no `BAD_LINK` from it), never
  reaches another address space, and is handled as a jump by that process to the address it names: the resident does not
  link, and enters through lookup and the normal fetch walk at the current CPL (a user address mapped by the process, or
  a page fault in it). A fault RIP derived from a forged slot affects only that process's own exception frame.
- A forged slot giving a non-canonical or kernel-half address reaches lookup as that process's #GP or #PF, never an
  assertion or `BAD_LINK`.
- Test: a CPL-3 program that stores into the slot (like the IF-byte test), including non-canonical and kernel-half
  values, and survives with only its own state changed.

**natemu, not FEX.** The rewrite needs only what the resident already keeps for each template (each guest-address
relocation's offset and register, the word count by decoding, the link points), is about the size of `relocate_code` and
replaces it for eligible blocks. fexc is unchanged, so its outputs stay byte-identical to the current series. The new code
gets its own gate: a static proof per block (each rewritten sequence decodes to "slot plus k" with k = the old constant
minus the page base; the only new store is the entry's `str` to that one guest-state field; nothing else changed), and a
check mode like today's reuse check (each instance's first run also compiled by the host at its address and compared
after normalising the rewritten sequences and the tail literal). Host admission still validates fexc's absolute object;
the rewrite is a resident step after it. A FEX code-generator change (base register or load in `EntrypointOffset` and the
exits) would reach any distance but changes every object, ends the byte-identical gate, and either reserves a register or
adds an instruction per constant; not needed for the measured problem.

**Memory.** Instances need their own area, not the metadata arena: with ASLR off, translations alone fill it (peak
134,217,700 of 134,217,728 bytes, one flush). Estimate: 15 M instances per vCPU over the run × about 48 bytes (record and
L2 slot) ≈ 720 MB; a 64 MiB area per vCPU (256 MiB of backing at 4 vCPUs) turns over about 11 times, each time dropping
only instances and cross-page links. Its place in the private window is to be laid out in Stage 3.

## 5. Alternatives

| Option | Effect (measured or estimated) | Cost and risk |
| --- | --- | --- |
| Larger metadata and index (`capacity-20261110-01`, measured) | Full build 294.5 → 260.7 s (-11%), flushes 31.5 → 16.5, slice 40.3 → 35.9 s (mean of three). The code area (128 MiB, branch range) now fills first, so this is near its ceiling | Small; compatible with everything below |
| Larger host answer cache (`GENERATION_BYTES`) | Unmeasured; bounded by the extra fexc time (+16.1 s per vCPU) and probably well under it, since fewer resident flushes also remove those compiles | Host memory only; cheap to try |
| Keep templates through a capacity flush (compact live templates, drop copies) | Not derivable from the data: with ASLR off, templates alone nearly fill the arena, and copies (about 9 M × 255 bytes) would still force frequent flushes, each with a compaction. Unknown; bounded by the host share (+33.8 s per vCPU without perf logging) | Medium; compaction of code and records |
| Reference-count copies by address space | Not feasible. The resident never sees an address space die: Linux frees page tables with no architectural event, CR3 values are reused, and without PCID nothing names an address space; it learns only lazily that a mapping moved (74 k "remapped" lookups per vCPU). Recency (`Block::checked`) is observable, lifetime is not | None to build |
| Deduplicate identical copies | Copies at different addresses differ in their constants by construction; copies at one address are already one block. Deduplication is what position independence does | Same as this plan |
| Denser records | Halving records halves flushes at most, like the capacity option | Medium, every record user |
| **Position-independent translations (this plan)** | See the estimate below | Larger: instances, checked cross-page links, entry and trust rules, check mode |

**Estimate for this plan.** Two ways, giving a range. (a) By the capacity calibration: requests should fall to about the
ASLR-off level (1.0 M; translations alone flush once or twice), 5.3 M fewer per vCPU; at 13 µs of wall each, about 70 s:
294 → about 225 s. This counts only flush-driven costs and is the lower bound on the gain. (b) By the cost rows, without
perf logging (gap 49.3 s per vCPU): host +33.8 s → about 0; copying +3.3 → about +1 (instance records); the placement,
fetch-walk and lookup row (+2.6 s) stays, since every new instance still misses, walks and is filed; linker +10.4 s →
somewhere between unchanged and the ASLR-off level, unknown until Stage 1 counts links by class; minus the 1-2 s tax. That
is 34-46 s, 70-93% of the CPU gap; scaled to wall time (crudely: configure is serial, so wall does not scale linearly with
per-vCPU CPU), about 200-220 s. Combined statement: 70-85% of the ASLR cost, 200-225 s, upper bound 186 s.

## 6. Staged plan

Each stage ends with the standing suites (`run_x3.py`, `run_i5a_faults.py`, `run_i5c.py` at both block sizes, `run_i6b.py`,
`run_boot_smoke.py`, `run_r1.py`, `run_k1.py`, the CPU/RAM harness) with the check mode on, and benchmarks by the
batch-end rules (one VM at a time under the VM lock, three interleaved rounds): the slice (`run.py NAME 4 slice`), the exec
loop (`run.py NAME 4 churn`), the same two with ASLR off (no regression from the tax), and the installer boot (the kernel
must not move). Baselines are measured again on the build each stage starts from (with the capacity change if merged).

1. **Counting, no behaviour change.** Per run: templates eligible for the rewrite and why others are not; links made by
   class (same-page, cross-page, from would-be-shared code into absolute or global code) and linked transitions by class
   (they price the cross-page check); frames seen at two linear pages in one address space and epoch; requests that a
   translation seen earlier in the run would have served (the bound on the host saving); host-cache drops followed by a
   recompile of the same answer. Stage 3 adds the cross-page check's hit rate and the counts of every slow path taken
   (check misses, slot lookups, blocks left unshared). Gate: eligible templates cover at least 90% of
   copies on the slice and the exec loop; otherwise revisit section 2's reach.
2. **The rewrite and entry, one instance per block.** Eligible templates are placed rewritten; copies keep being made, now
   by plain copying of rewritten code. This proves the new code and measures the tax alone. Gate: unit tests of the
   rewrite's proof (each refusal red under its mutation, as for `relocate_code`); a stop on each entry word and the body
   entry, checking the RIP each reports; the link forms of section 3 (arm and disarm on the `svc`-word form,
   `links_restored` with both forms, absolute-to-shared links, a precise stop at a linked `svc` word with the RIP from
   x0); slot writers only the two named; a run with x86 AC on; the CPL-3 slot-store test; tail literal unread (test);
   suites green; check-mode installer and slice with zero mismatches; ASLR-off slice within 2% of before.
3. **Instances.** L2 and L1 owners name instances; instances name shared translations; per-instance currency, linear
   chains and canonical checks; the instance chain and its use in every kill path; checked cross-page links; copies stop for
   eligible blocks. New K1 cases, each red under its mutation: one frame at two linear pages of one address space,
   entered at the second page through an L1 entry and through a local link, with a cross-page link from the first; the
   same address in a forked child; INVLPG of one instance; a code write and an I/O drain while a second instance has a
   live L1 entry; a link from an absolute block into a position-independent target on the same page; a shared block on
   the same page and frame as an absolute one, run in a second process at another address (the shared-to-absolute
   case); a fault and a linked exit in a block reached only by a local link (slot accepted through the source's
   instance); a global TLB flush with checked links live; a global mapping found for a shared instance; instance eviction
   with live L1 entries. Gate: those, suites green, slice at most 35 s and exec loop at most 2.5 ms, then one full build.
4. **Instance-area eviction and tuning.** Sizing from Stage 3's counts. Gate: full build three rounds, capacity flushes at
   most 3 per vCPU, host requests within 1.5x of ASLR off.

Stop rules: if Stage 1 shows eligibility under 90%, or Stage 2's tax exceeds 3% of guest time, report before going on.

## 7. Risks

- **Shared links**: cross-page links rely on L1 entries being exact for the epoch and on undoing links into a table that
  loses its context; the two-mapping K1 cases, a global flush with checked links live, and the check mode are its tests.
- **Invalidation completeness**: every kill and eviction path must walk the instance chain.
- **The base slot is guest-writable**: contained by section 4's rules; the resident must also never clobber it while a
  block can resume mid-way (fault retry).
- **Two code forms coexist** (absolute and position-independent); every resident path that reads a block address must
  say which it uses. Section 2's inventory is the checklist.
- **Memory**: the instance area adds backing per vCPU.

## Open questions for the owner

1. Approve the natemu-side rewrite (FEX unchanged, byte-identical gate kept) over a FEX code-generator change?
2. Accept an estimated 1-3% guest-time tax on relocatable user code and about 64 MiB more backing per vCPU for an
   estimated 70-85% recovery of the ASLR cost, on top of the capacity change?
3. Merge the capacity change first, so that Stage 1 counts on that baseline?

# Position-independent translations: design study

Status: design and evidence only, November 10, 2026, on `codex/virtio-io` at 6ac050e. Nothing is built. Owner sign-off is
needed before any implementation. Inputs: the CPython profile
(`natemu-raw/cpython-profile-20261110-01/`: `summary.md`; `runs/full-off-1` and `runs/full-norand-off-1`, the `vcpu` lines
of `perf-trim.log`), [perf-plan.md](perf-plan.md) chunks 3, 4, 8 and 10 (the relocation proof, host reuse, local links,
reuse inside the resident), `crates/runtime/src/cache.rs`, `crates/abi/src/execution.rs`, and the FEX fork's code
generator (`third_party/FEX`, `natemu` branch). Estimates are labelled as estimates and show their arithmetic.

## Recommendation in one paragraph

Build position-independent translations, but do it in natemu, not in FEX: when the resident places a block that is
already a reuse template, it rewrites each guest-address constant into "load this block's page base, add an offset", in
exactly the same number of instructions, and adds a two-instruction entry that records the page base. FEX's output, and
its byte-identical gate, stay as they are. One translation then serves every virtual address with the same physical
page, page offset and context; each address gets only a small "instance" record (about 48 bytes instead of a copy of about
425 bytes of code and metadata). Every library and every relocatable executable in the guest image fits the rewrite (all
span less than 16 MiB; the large programs, `cc1` and `python3.13`, are loaded at fixed addresses and need no
relocation). Estimated result: 85-90% of the cost of guest ASLR removed (full build about 200 s against 290 s now and 186 s
with ASLR off). The cheaper alternative, keeping templates through a capacity flush, is estimated at about half. A larger
cache (the separate capacity experiment) helps by itself and is compatible with either. Proceed in four stages, each with
its own test and benchmark gate (section 6).

## 1. What ASLR costs now

Per vCPU, mean of the four vCPUs, 4-vCPU full build (ASLR on minus ASLR off; seconds of vCPU thread CPU time):

| | ASLR on | ASLR off | difference |
| --- | --- | --- | --- |
| vCPU CPU time | 190.7 | 136.8 | +53.9 |
| guest code | 77.9 | 75.6 | +2.3 |
| host (translation requests) | 48.1 | 9.8 | +38.4 (of which fexc +16.1, admission +5.2, perf log +4.6) |
| linker and patching | 27.2 | 16.7 | +10.4 |
| copying templates (`reuse`) | 3.3 | 0.0 | +3.3 |
| placement, fetch walks, lookup, entry | 10.6 | 8.0 | +2.6 |
| requests to the host | 6.27 M | 1.00 M | |
| blocks from the host / copies | 6.13 M / 8.90 M | 0.89 M / 0.04 M | |
| capacity flushes | 28-32 | 1-2 | |
| linker entries | 42.3 M | 29.5 M | |

Mechanism: each new process maps the same library pages at new addresses. The resident serves most of them by copying a
template and rewriting its constants, but every copy is a new block with its own code and record (about 170 bytes of code
and 255 of metadata on average: 3.6 GB written into a 128 MiB arena over the run). The arena fills about 30 times; each
flush drops the templates with the copies, so the next process's code goes to the host again (6.27 M requests instead of
1.0 M), and every copy's exits are linked again. The guest's own work barely changes. The answer is not more
copy speed, it is fewer per-address bytes.

## 2. Where translated code depends on the absolute guest address

FEX already builds every guest address from the block start: its IR names them as "entry point plus offset"
(`EntrypointOffset`, `InlineEntrypointOffset`, `GetRelocatedPC` in `OpcodeDispatcher.h`); only its back end turns them into
absolute constants. The inventory, with what each becomes:

| Dependence | Where | Today | Position-independent form |
| --- | --- | --- | --- |
| Next-instruction and branch-target addresses, call return addresses pushed, RIP-relative operands (`lea`, loads, stores) | `EntrypointOffset` → `InsertGuestRIPMove` (relocation kind 3, 1-4 move words) | `movz/movk xR, #absolute` | `ldr xR, [x28, #page_base]` then 1-3 `add/sub xR, xR, #imm` (same word count) |
| Literal exits (link points) | `BranchOps.cpp` exits: move into x0, `svc #LINK` | linker rewrites the move's first word to `b target` | same rewrite of the move; linker patches by link class (section 3) |
| Code-tail address literal | `JIT.cpp` tail, relocation kind 1 | 8-byte slot | left as the translation's address; nothing in natemu reads it (perf chunk 10 found no guest-visible read; to be asserted) |
| Access sites, PC table, split stubs | `Site::guest_offset` etc. | offsets from the block address | unchanged; the resident adds the instance address |
| Block address in the record, lookup keys (L1 code entries, L2) | `Block::rip`, `fill_l1`, `hash` | one block per address | per-instance address in an instance record; L1 entries unchanged (they compare the address and branch to the shared entry) |
| Fault and exit address (resident) | `execution.rs` uses `b.rip + guest_offset` | block address | page base from the guest-state frame plus the translation's page offset |
| Breakpoints, stop address, hook fences, RF and single-step | `code_breakpoints`, `reuse_fenced`, step slot | compared with `b.rip`; templates refused under DR7 | compared with the instance address; the same refusals apply when an instance is made; the step block stays absolute |
| Currency (TLB epoch, GLOBAL, LARGE), linear chains for INVLPG | `Block::checked`, `flags`, `lnext` | per block | per instance |
| Frames, context, code, sites, death by a code write | `Block::source_gpa`, `DEAD` | per block | per translation (shared) |

Instruction cost per address constant: today 2-4 words (3 for a 47-bit user address); rewritten, the same count, with one
load from the guest-state page in place of the first word. A RIP-relative access then has a load-then-add chain of about 6
cycles where the move chain had about 3 (an estimate; Stage 2 measures it). Per block entry: two more instructions (`and`
to the page, `str`). Per cross-page linked exit: the address computation now executes (2-3 instructions) where the link
skipped it. No register is reserved: FEX has only 7 allocatable general registers here (x18, x20-x24, x30; x25 holds the
saved flags, x26/x27 PF/AF, x28 the state), so taking one would spill; the base lives in the guest-state page, like the
frame slots natemu already added there (`KERNEL_GS_BASE`, `VERW_GDT_BASE`). Estimated tax: 1-3% of guest time, about
1-2 s per vCPU of the 78 s.

**Why the page base and not the block address.** A link between two blocks that start on the same page and frame
leaves the base unchanged, so it stays a single `b` past the target's entry instructions, as cheap as a link today.
With a per-block base every link would have to recompute it.

**Reach of the same-length rewrite.** Three words reach ±16 MiB from the page base (`ldr`, `add #imm, lsl #12`,
`add #imm`); a two-word move only ±4 KiB or a page multiple. A position-independent image cannot refer by direct branch or
RIP-relative operand outside itself, so its span bounds every offset. Evidence (throwaway script
`natemu-raw/pic-20261110-01/spans.py`, output `spans.out`, run over every x86-64 ELF in the profile's initrd, 681 files): all 234 relocatable executables and 430 of 431
shared libraries span less than 16 MiB (the exception is `libtsan`, 16.6 MiB, not used by the build); the three larger
images (`cc1` 34.4 MiB, `lto1`, `lto-dump`) and `python3.13` are fixed-address executables, which ASLR does not move, so
they keep absolute code. Blocks whose rewrite does not fit (a two-word move of a far target, kernel code, a step) stay
absolute and are copied as today.

## 3. Links

A link patched into a shared translation applies to every instance of it. A direct link from A to B is correct only if,
for every instance of A that can run while the link is there, A's target address maps to B's frames at that page offset
and B is current there.

- **Local links** (target starting on the source's page, same frame, same context; includes self links, REP self exits and
  the STI case) hold for every instance by construction: the instance runs only through a current mapping of that page.
  They need no validation by address space, survive CR3 loads and TLB flushes as local links do today (perf chunk 8), and
  go only when the translation dies. They become the common, cheap case: unlike today, they also survive the next
  process.
- **Cross-page links** depend on another page's mapping. Rule: **one linkable instance per translation per TLB epoch.**
  A translation records the instance address its outbound cross-page links were made for. They are pruned, as today's
  non-local links are, at a CR3 load (unless the target is global) and at a TLB flush, when the target instance is
  invalidated (INVLPG through the instance's linear chain), and in addition when another instance of the same source
  translation becomes current in the same epoch. Every way into code goes through a lookup or an L1 entry filled by one
  (an instance made current in this epoch) or through a link made under the rule, so the check sits on one resident path.
  Cheaper schemes are rejected: a per-link address compare in the exit needs 4-6 more words than the rewritten exit has;
  sending cross-page exits through the dispatcher costs an indirect branch per transition.
- **Kernel and other absolute translations**: unchanged (one address each).

Other events: a write to a code page kills the translation (all its instances, which find it dead at lookup) and prunes
links into and out of it, as now; the windowless I/O code-page drain acts on frames, so on translations, as now; a capacity
problem in the instance area drops instances and cross-page links only, keeping translations and local links; a full
flush is needed only when translations fill the arena (estimated 1-2 a run, the ASLR-off count). Instances are not made
while an instruction breakpoint is armed, the same refusal templates have today.

## 4. Where the work lands

- **natemu, not FEX.** The rewrite needs only what the resident already keeps for each template (each guest-address
  relocation's offset, register and, by decoding, word count; the link points). It is about the size of
  `relocate_code` and replaces it for eligible blocks. The two entry instructions go in the code header word and one
  shifted word, or the body moves by 16 bytes (every branch inside a FEX block is PC-relative; sites, link points, the
  computed exit and relocations shift together; alignment modulo 16 is kept). Nothing in natemu reads the header word.
- **FEX's gate stays.** fexc is unchanged, so its outputs stay byte-identical to the current series. The new code gets its
  own gate: a static proof per block (each rewritten sequence decodes to "base slot plus k" with k = the old constant minus
  the page base; nothing else changed), plus a check mode in the spirit of today's reuse check (each instance's first
  run also compiled by the host at its address, and the two compared after normalising the rewritten sequences).
- **Admission.** Host admission still validates fexc's absolute object as today; the rewrite is a resident step after
  it, checked like the copy re-check is now. Admission rules that read x28 are unaffected (the new load reads x28; nothing
  writes it).
- **A FEX code-generator change** (base register or load in `EntrypointOffset` and the exits) would reach any distance but
  changes every object, ends the byte-identical gate, and either reserves a register or adds an instruction per
  constant. Not needed for the measured problem.

## 5. Alternatives

| Option | Effect on the 53.9 s per-vCPU gap (estimate) | Cost and risk |
| --- | --- | --- |
| Larger metadata and index (separate experiment, `natemu-raw/capacity-20261110-01/`, in progress) | The code area is capped at 128 MiB by branch range and peaks at 85 MB when the arena fills, so about 1.5x is the ceiling. Slice, three rounds each (preliminary, the experiment's own report pending): 39.2-41.9 s before, 35.7-36.0 s after, 32.9 s after with ASLR off; so about half of the slice gap | Small; compatible with everything below |
| Keep templates through a capacity flush (move live templates to the arena's start, drop copies; choose by `Block::checked`, the last epoch a lookup revalidated the block) | Most of the extra host requests go (+38 s → about +8 s), but more copies are made (~13 M at ~0.37 µs) and relinked; net about 26 s, half the gap | Medium; compaction of code and records, rebuilt chains |
| Reference-count copies by address space | Not feasible. The resident never sees an address space die: Linux frees page tables with no architectural event, CR3 values are reused, and without PCID nothing names an address space. It learns only lazily that a mapping moved (74 k "remapped" lookups per vCPU). Recency (`checked`) is observable, lifetime is not | None to build |
| Deduplicate identical copies | Copies at different addresses differ in their constants by construction; copies at the same address are already one block. Deduplication is exactly what position independence does | Same as this plan |
| Denser records | Records average 255 bytes; halving them halves flushes at most, like the capacity option | Medium, touches every record user |
| **Position-independent translations (this plan)** | Host requests back to about ASLR-off (+38 s → about 0); copying gone (+3.3 → about +1 for instance records); linker entries move toward ASLR-off as local links outlive processes (+10.4 → +3 to +6); minus the 1-2 s tax. About 45-50 s, 85-90% of the gap | Larger: resident cache structure (instances), link rules, check mode |

Arithmetic for the wall-clock estimates: the full build's wall-clock gap is 104 s for a CPU gap of 54 s per vCPU. Scaling:
templates kept, about 290 - 0.5 × 104 ≈ 238 s; position independence, about 290 - 0.87 × 104 ≈ 200 s; upper bound 186 s
(ASLR off). The slice (40 s, 33 s with ASLR off) and the exec loop (`/bin/true` 5.4 ms, 1.6 ms with ASLR off) should move by
the same fractions. Instance arithmetic: 15 M instances per vCPU × ~48 bytes (instance record plus L2 slot) ≈ 720 MB over
the run, so an instance area of 64 MiB turns over about 11 times; each turnover drops only instances and cross-page links.

## 6. Staged plan

Each stage ends with the standing suites (`run_x3.py`, `run_i5a_faults.py`, `run_i5c.py` at both block sizes, `run_i6b.py`,
`run_boot_smoke.py`, `run_r1.py`, `run_k1.py`, the CPU/RAM harness) with the check mode on, and benchmarks by the
batch-end rules (one VM at a time under the VM lock, three interleaved rounds): the slice (`run.py NAME 4 slice`), the exec
loop (`run.py NAME 4 churn`), the same two with ASLR off (no regression from the tax), and the installer boot (the kernel
must not move).

1. **Counting, no behaviour change.** Count, per run: templates eligible for the same-length rewrite and why others are
   not; links made by class (local, cross-page, into global code) and linked transitions where countable; how often a
   second instance of a translation would be current in one epoch. Gate: eligible templates cover at least 90% of copies on
   the slice and the exec loop; otherwise revisit section 2's reach.
2. **The rewrite and entry, one instance per block.** Eligible templates are placed rewritten; copies keep being made,
   now by plain copying of rewritten code (no constants change). This proves the new code and measures the tax on its
   own. Gate: unit tests of the rewrite's proof (each refusal red under its mutation, as for `relocate_code`); suites
   green; check-mode boot of the installer and the slice with zero mismatches; ASLR-off slice within 2% of before.
3. **Instances.** L2 points to instances; instances to shared translations; per-instance currency and linear chains;
   the link rules of section 3; copies stop for eligible blocks. New K1 cases: one frame mapped at two addresses in one
   address space with a cross-page link (the one-linkable-instance rule; red without it); the same address in a forked
   child; INVLPG of one instance; a code write with live instances; the I/O drain with live instances. Gate: the above
   red under mutation, suites green, slice at most 35 s and exec loop at most 2.5 ms (each at least two-thirds of the
   ASLR gap closed), then one full build.
4. **Instance-area eviction and tuning.** Instance overflow drops instances only; sizing from Stage 3's counts. Gate: full
   build three rounds, capacity flushes at most 3 per vCPU, host requests within 1.5x of ASLR off.

Stop rules: if Stage 1 shows eligibility under 90%, or Stage 2's tax exceeds 3% of guest time, report before going on.

## 7. Risks

- **Correctness of shared links** is the main risk; the one-linkable-instance rule must be enforced on every path that
  makes an instance current. The check mode and the two-mapping K1 case are its tests.
- **Base slot consistency.** The resident must never clobber the base slot while a block can resume mid-way (fault
  retry), and must read it only for position-independent translations.
- **Tax on code that never relocates** is avoided by rewriting only templates (non-global, the host's template rule);
  fixed-address programs and the kernel are untouched.
- **Interaction with the capacity experiment**: none in design; its larger arena reduces flushes for both forms.
- **Two code forms coexist** (absolute and position-independent); every resident path that reads a block address must
  say which it uses. The inventory in section 2 is the checklist.

## Open questions for the owner

1. Approve the natemu-side rewrite (FEX untouched, byte-identical gate kept) over a FEX code-generator change?
2. Accept a 1-3% guest-time tax on relocatable user code for the expected 85-90% recovery of the ASLR cost, given
   that keeping templates through a flush (no tax, about half the gain) is the cheaper alternative?
3. Should Stage 1's counting run before or after the capacity experiment is merged (its larger arena changes the
   baseline)?

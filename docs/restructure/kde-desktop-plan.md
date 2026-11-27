# A faster KDE desktop: plan

Status: draft 6 (patched after code audit, November 27, 2026), with [kde-desktop-spec.md](kde-desktop-spec.md): the goal and what today's numbers
do and do not show (1, 1.1), the deliverables and barriers (2), the testing and benchmark cadence (3), management (4) and
the combined code store's requirements R1-R7 and decisions D1-D14 (5). Nothing here starts before the owner agrees.
Branch names are local; nothing is pushed without the owner. When the owner agrees, both documents become the first
commit of the implementation line, with `shared-code-plan.md` reconciled from `mmio-flush-return` (chunk 5 and MMIO return),
`shared-code-regions` (`13cd1ae1`, chunk 2 review and M2 Max replication), `shared-tail` (`73a8b354`, chunk 3 as-built and
paired-stop sweeps), and `tail-owned-recovery` (`e366e24f`, private-cache prerequisite, dropping its draft placeholders).

**Goal:** the KDE desktop scorecard clearly better than `main` at M1, better again at M5, cold first, with no standing
benchmark worse except an accepted trade-off.

**Architecture:** land P1 + P3 + caps once their crash is fixed; measure what the cold rows spend their time on and
choose the next investment; by default bring the code store's four branches onto that base as one line and build the
store in steps, pooled between the barriers M4 and M5.

**Tools:** Rust (`crates/abi`, `crates/runtime`, `crates/host`), `tools/natemu-test` for every test and number, the Mac
mini M4 for numbers, the M2 Max, M3 and rented M4 for builds and correctness runs, the x86 box for amd64 builds and the
KVM reference.

Each step names its **base**, **work**, **tests** (each new check shown to fail when it should) and **done when**; a
decision run is named where a step decides something. Tripwires, smoke runs and barriers follow spec section 3. Sizes
are estimates of changed code lines, provisional until the barrier, the tail's stop oracle and S6's patch ownership are
designed.

---

## M1 Base landed, next investment chosen (a barrier)

### L0 The work branch

A new work branch `m1` from `b39c4393`. (`land-kde1-fix` stays where it is: `f498d2e5`, an earlier stage.)
`land-kde1-gate` stays at the gated `b39c4393` until `m1`'s exact tip is gated.

### L1 The crash's cause — done

Spec section 6: `diag-badstop` `e02b84ea`, run `20261126-215633-c8dc` on the M4: kind 3, a live guarded owner with a
24-byte body, the PC in its stub, `block_at` finding nothing. The two earlier halts used the first diagnostic
(`01ae36a1`) and are not evidence either way.

### L2 The fix and its regression

- **Work:** `block_at` finds the record whose executable extent (guard stub, code, hot copy) holds the PC, by a search
  keyed on extent starts rather than code addresses with a fixed lookahead; the step slot, hot copies and dead owners are
  found as before.
- **Files:** `crates/runtime/src/cache.rs` (`block_at`, filing); a K1 program in `tests/system/run_k1.py`.
- **Tests:** K1 `guard-stub-records`, two layouts: a short guarded block followed by two records without code; and one
  followed by a record without code and then real code at the next aligned address. In each, the PC hint missed and
  guards on (not the PIC stop mode); an exact SGI at every reachable stub word on a guard hit and a miss and at the body's
  first word; the observed stop address and banked state equal to a stop at the entry. The case asserts its layout and is
  shown to fail with the old search.
- **Then, predeclared:** six desktop case runs on the fixed build (three of each desktop case, on the M2 Max and the M4),
  every one counted; any `BAD_STOP` fails L2.
- **Done when:** the K1 case passes and fails as stated, the six runs are clean, the record is in
  `frame-keyed-plan.md` section 12.

### L3 The desktop cases: strict, and plain-build variants

- **Work:** `desktop-kde-profile` and `desktop-kde-browser-profile` already fail when the driver does not reach its end or
  a phase has no windows (the crashed runs of November 26 did). Check the remaining conditions (a missing row, a phase
  timeout, a failed VM exit, a session that never reaches idle) and add only those missing; add one that a UI action took
  effect (the window's geometry or the active window after it: KWin may return from a switch without switching). A
  test-framework change: each added check shown red once on a broken input, the UI check on a no-op action with complete
  markers. Then apply the checks offline (`run_desktop.py`'s `analyse`) to the stored base runs before reusing them.
  Add plain-build variants of both cases (ordinary resident, no request log; spec 3.1), each run once to show it reports
  every row.
- **Files:** `tests/bench/run_desktop.py`, `tests/bench/family.toml` (desktop suite).

### L4 Guard-cost, chosen before the scorecard (a decision run)

- **Work:** the guard local-skip commits on `m1` after L2. Its cold Chromium start (`desktop-kde-browser-profile`) and
  `guest-build` against L2's commit, by the noise rule (spec 3); L2's six predeclared runs repeated on it. It joins `m1`
  only if it is better beyond noise on both and crash-free; inconclusive leaves it out. (Note: if M2–M5 proceeds, S2
  disables cross-page guarded links for shared-capable blocks per D3, so local-skip is an M1 gain unless a
  generation-aware guard is built later.)

### L5 The barrier's numbers and the next investment

- **Scorecard** (spec 3.1): `m1`'s tip against the stored base runs at `b168570a` (`84bd`, `fd58`), the existing desktop
  cases and their `-warm` variants; a row's noise from its stored repeats, one more base run only for a row with none;
  the cold start rows that make "clearly better" confirmed with L3's plain-build cases, one run a side.
- **Breakdown** (`perf` build, the same runs): on each cold start row, host time by request kind (first-seen
  compilation and admission, repeated requests, device I/O); requests and placement for blocks a peer held live,
  counted at request time by a diagnostic counter that applies D1's execute rule (the request trace alone cannot tell),
  or marked unknown; CR3 load and refill cost in the serial
  stretches; the trace's dropped records checked. Each candidate investment (the store, a bounded first-seen translation
  change, process-switch work) gets a range of wall time it could plausibly remove, with the overlap stated.
- **If the ranges cannot tell the candidates apart,** a bounded A/B (a prototype of the leading candidates on one cold row)
  settles it before M2 starts. References (spec 3.1) are measured here only if the choice needs them.
- **Done when:** the scorecard (every row's verdict) and the breakdown are in `frame-keyed-plan.md` section 12; every
  worse row is named as a trade-off for the owner (spec 1).

### L6 The standing set

- **Work:** the standing set in full (spec 3.2) at `m1`'s tip; the base side reused where the M4 has stored runs of
  `be88e684`'s cases with the same options, run once where not. `docs/benchmarks.md` updated with every number that moved (known:
  `debug/web-tooling-profile` about 4% slower, in the diagnostic record), the io5 row moved into a Whole-system workloads
  section and a Desktop experience section added from the plain-build cases (spec 3.2).

### L7 The gate, the PR and the choice

- **Work:** the gate at `m1`'s exact tip (a mosaic allowed); `land-kde1-gate` moved to it; the owner's approval of L5's
  scorecard, L6's numbers and the named trade-offs; the PR. The owner chooses the next investment from L5.
- **Done when:** `main` holds the commit. **M1 ends here.** If the owner chooses something other than the store, M2-M5
  wait for that work's own plan.

---

## M2 One store line (not a barrier)

Base: M1's commit on `main`. Work line `p2-store`. Behaviour unchanged. Tripwire and smoke per batch (spec 3).

### I1 Primitives (chunk 1)

- **Work:** merge `shared-code` (it merges cleanly on `b39c4393`).
- **Tests:** `tools/natemu-test run tests/unit -k rust/all`, the `shared` tests among them.

### I2 Regions, the store's spans and the budget (chunk 2, D8, D9)

- **Work:** merge `shared-code-regions` (10 conflicting hunks, about 260 lines, in `crates/abi/src/execution.rs`,
  `crates/host/src/execution.rs`, `crates/runtime/src/cache.rs`, `crates/runtime/src/execution.rs`,
  `tests/system/run_k1.py`), keeping both sides; place `REGION_TABLES_OFFSET` after P1's `LINKS_OFFSET`; keep chunk 2's
  64 KiB `TAIL` slot after `PROBES` (preserving `SLOT_BYTES = 64 KiB` and `NATEMU_STORE_TEST`); route P1's
  region-dependent sites through `head_at` / `head_offset` / `head_pc` (`guard_words`'s `b GUARD_MISS`, `returned()`'s
  `GUARD_MISS` check, `link()`'s `in_some_form`, and every `DISPATCH`, `GUARD_MISS` and `destination(` use the merge
  brings in); rewrite P1's link-span assertion; check a guarded link into another region; implement the memory budget
  (D9) for the store, each vCPU's private state, the pool and the answer cap, and the behaviour when one region does not
  fit.
- **Tests:** K1 cases executing in region 3 of four, asserted by the PC: a missed guard through its trampoline and a
  checked link, shown to fail with the `guard_words` conversion reverted; budget unit tests over the vCPU count, private
  and store allocations together, 8 GB (the M3), 16 GB and 64 GB hosts, and the below-one-region outcome.

### I3 MMIO flush-return fix and private flush harness (chunk 5 prerequisites)

- **Work:** merge `mmio-flush-return` (`mmio::ones` pre-classification `BankSite` snapshot and `flush_pending()` precise
  step, D14; the `shared-flush-private` harness and `shared::Barrier` unit schedule). Note: `mmio-flush-return` has no
  runtime `shared::Barrier` wiring (and P1's `cache::reset()` already clears `FKEYS`, `CHECKED_AT`, `ENTERED_AT`,
  `TL1_PAGES`, `CTLB`, segment slots and L1 owners); the VM-wide runtime barrier itself is built in S3b.
- **Tests:** `mmio-flush-return`, `shared-flush-private` and the unit schedule.

### I4 Private-tail stops (chunk 3, D5-D8)

- **Work:** merge `shared-tail` onto chunk 2's 64 KiB `TAIL` slot after `PROBES` (keeping `SLOT_BYTES = 64 KiB`, not
  `shared-tail`'s 32 KiB `SLOT`/`TAIL_SLOT` split); port `tail-owned-recovery`'s pre-reset banking fix with its overwrite
  test; update `cache::tail()` so head branch targets from regions `r >= 1` map into `TAIL`'s reach (`head_offset` at
  region 0's `TAIL_VA`, or `head_at(code, TAIL)`); move `TAIL` off bit 24 and clear `GUARDED` on the synthetic tail
  record; handle stops in a guard stub (D5) and in a shared translation by the RIP actually entered (D6). Run the four
  paired-stop observer sweeps (`shared-tail-observer`, `rep-stop-pair`, `ls-stop-pair`, `x87-stop-pair`) as a one-time
  I4 verification against the pre-I4 integrated commit (recorded in the plan), rather than keeping commit-pinned
  `git archive` source-patching scripts on `p2-store`/`main` (which break under rebase/squash and later `execution.rs`
  edits).
- **Tests:** an exact SGI at every reachable stub word on a guard hit and a miss; a translation entered at two RIPs, with
  REP and an interrupt shadow; a private-tail stop and a probe return in region 3 (requiring `REFUSED == 0`); the
  paired-stop sweeps rerun at a guarded block, a checked link and a shared translation (evidence recorded, not
  commit-pinned in `main`).

---

## M3 Store in use, private (S1-S4b; not a barrier)

Each step's design is reviewed before its code (Fable or Astra), its code by a model other than its author; tripwire and
smoke per batch.

### S1 One table, find and execute (D1, D2, R7) — about 700 lines

- **Work:** `fkeys`' storage becomes `shared::Table`'s (one instance per vCPU in its lookup area) under a remixed hash;
  template and execute matches separate (full RIP for absolute code; both frames for crossing code; alias, canonicality
  and compat32 checks for PIC); blocks named by record offset (0 reserved, 16-byte aligned); P1's private link records by record
  offset; deletion replaced by generations; a record published only after relocation, code synchronization, sites and
  the final page check, with a readiness word a reader checks.
- **Files:** `crates/abi/src/fkeys.rs`, `crates/abi/src/shared.rs`, `crates/runtime/src/cache.rs` (`file`, `template`,
  `copy`, `copy_pooled`, `share`, lookup), `crates/runtime/src/execution.rs` (`find_from`, `miss_fast`, `reused`).
- **Tests:** the merged `fkeys`/`shared` unit tests; K1's frame-keyed programs; mutations caught: execute on a template
  match, a skipped generation check, a record readable before it is ready; a hash-collision unit test.

### S2 One link rule (D3) — about 400 lines

- **Work:** `link()`'s forms reduced to local (the `local` predicate, same region) and checked, with P1's PIC-alias and
  STI-first restrictions kept; guarded links between pages and step 1d's plain global links off for shared-capable
  blocks; `prune`/`suspend`/`relink` kept only where still needed.
- **Tests:** K1's link programs rewritten to the rule (`relink-global-many`, `link-two-forms`, `guard-local`); caught
  mutations: a plain global or guarded cross-page link left in; an alias-remap case; a PIC source entered at two aliases
  with an absolute target; the existing held-event STI case.
- **Decision run:** `guest-build` and the cold Chromium start against S2's immediate parent (S1's tip), so the link
  rule's cost is not mixed with S1's and I4's changes. A cost beyond noise is either an
  accepted trade-off (spec 1) or needs a design before S5 (a generation-aware guard, a supervisor code TLB).

### S3 Per-vCPU state out of the records (shared-code plan chunk 6) — about 500 lines

- **Work:** `Block::checked`, the walk's `GLOBAL`/`LARGE` and `lnext` move to the private L1 owner lists.
- **Tests:** K1's TLB programs (INVLPG, CR3, PGE toggles, #PF of a code page) at 1-4 vCPUs.

### S3b VM-wide flush barrier (`shared::Barrier`, D14) — about 500 lines

- **Work:** wire `natemu_abi::shared::Barrier` into the resident, driving today's private resets together across vCPUs
  before anything is placed in the shared store: `away` published before every host exit (`ask`, `mmio::page`,
  `mmio::device`) and every resident wait (`io_checkpoint`, `HLT`, AP start-up) and cleared with a `SeqCst` request check
  on return; requester wait loop serving `take_shadows`; peer wait loop (`while !over(e)`) also serving `take_shadows` so
  a mid-block peer waiting in `split_begin`/`remap_shootdown`/`mark` cannot deadlock the barrier (R5); peer
  acknowledgement and release wait covering P1's full private reset (`FKEYS`, `CHECKED_AT`, `ENTERED_AT`, `TL1_PAGES`,
  `CTLB`, segment slots, L1 owners, tail); host-initiated flush window coordination; `mmio::ones` checking barrier
  catch-up on return as well as local flush posts, and `mmio::device`'s poll-loop `take_invalidations` handled without
  touching shared store state while `away = 1` (R5).
- **Tests:** `shared-flush-private` upgraded to require peer resets on leader capacity flushes (with peers at HLT,
  completed non-idempotent MMIO read, and posted compile, plus a peer parked across multiple epochs); `mmio-flush-return`;
  section 9's deadlock mutations (a parked vCPU not marked away; `away` not re-checked on return) plus a 3-vCPU barrier +
  mid-block shadow-drop deadlock test and its mutation (no `take_shadows` in the peer `while !over(e)` loop).

### S4a Placement into the store, owner only, with reclamation (D12-D14) — about 700 lines

- **Work:** every vCPU places into, files and publishes in the store, executing only its own blocks; per-chunk PC
  ownership index entry (with release store of chunk count) published before the L2 CAS (R7) and used by `block_at`, the
  host's audits and symbolization; the writable private head; adoptable blocks'
  literal exits in D12's interim form; store reclamation through S3b's VM-wide barrier when the store fills, with forward progress
  for a requester that loses the freed space (5.2's open question, designed here).
- **Tests:** the system family at 1-4 vCPUs; a block placed by vCPU 1 never entered by vCPU 0 (counter); `block_at`
  against a full scan at every K1 stop; a store-full reclamation with a peer running, a peer parked through several
  epochs, a held host reply and a completed non-idempotent MMIO read; a requester losing the freed space must complete;
  section 9's deadlock mutations.
- **Decision run:** D12's interim form against linked exits, on the cold start rows and `guest-build`.

### S4b The private code rows removed — about 300 lines

- **Work:** the per-vCPU code row and its write alias removed; a writable private alias of the head kept.
- **Tests:** the system family; the host's audits over the store only.

---

## M4 Shared code (S5; a barrier) — about 900 lines

- **Work:** a vCPU enters a peer's live block (the execute match, the marks and bit checks of shared-code plan section 6,
  its L1 fill); dispatcher slots VM-wide (D10); compiler specializations never adopted across a mismatch, without
  offering the same block again and again (D11); adoptable blocks in D12's interim form; a page's generation kills its
  blocks for every vCPU on guest stores and host DMA (R4), using `clear` → `bump` for pre-store shadow writes (`written`)
  and `fence(SeqCst)` → `bump` → `clear` for post-store rechecks (`rechecked` and windowless host DMA's post-copy
  recheck), with a safe epoch rollover (5.2's open question, designed here).
- **Tests:** section 9's stale-code and write-ordering mutations; a competing reader of a record not yet ready;
  self-modifying code at 2-4 vCPUs; host DMA into code pages, parked and live, and windowless DMA / `rechecked` racing
  an adopter between `bump` and `clear` (plus the mutation running `clear` before `bump` on the post-store recheck);
  P3's prefaulted writable neighbour during an adoption; a specialization mismatch that makes forward progress; slot
  acquisition in opposite orders and exhaustion; a near-wrap generation unit case; stress at 4 vCPUs.
- **Barrier:** the gate at its exact commit; the scorecard against M1; adoption on and off on the same layout; adoption
  counters with L5's eligibility rule; the process-heavy standing group.
- **Decision (go / no-go for M5):** adoptions replace most eligible requests a peer held live, and the cold start rows
  move.

## M5 Store complete (S6; a barrier) — about 700 lines

- **Work:** D4's concurrent patch ownership written, reviewed, built: lock-free links in shared code with no shared link records, made once for every vCPU, legal words
  only at a trampoline or `svc` word (R2), the `synced` rule for every direct edge; the per-vCPU code path deleted; the
  store on by default.
- **Tests:** section 9's link mutations; the paired-stop sweep at a link made by a peer.
- **Barrier:** the gate; the scorecard against M1 and `main` (with ordinary-resident cases for the page, and the
  references); the standing set in full; `docs/benchmarks.md`; spec 1.1's table redone with L5's breakdown.
- **Done when:** the owner approves the PR. **The project's deliverable.**

---

## Machines

| When | M4 | M2 Max | M3 | Rented M4 | x86 box |
|---|---|---|---|---|---|
| M1 | L2's and L4's runs, L5, L6 | L2's runs, builds, gate | gate (AFP units, devices) | gate | KVM reference if needed |
| M2-M3 | tripwires, decision runs (S2, S4a) | builds, tests, smoke | tests (AFP units, devices) | tests (system, faults) | amd64 builds |
| M4, M5 | barrier runs | gate, builds | gate | gate | amd64 builds, references |

The M4 runs only timed cases, alone; its disk is kept above 15 GB free before a desktop run (each leaves about 2.5 GB).

## Risks

- **L5 ranks another investment first.** The store waits; the owner chooses.
- **A regression enters between barriers.** The tripwire catches large ones per batch; the barrier's A/B catches the rest.
- **S2's link rule or D12's interim form costs too much.** Their decision runs come before S5.
- **Adoption gains less than the one measured row suggests.** M4's counters and scorecard decide before S6.
- **A coordinator outage.** Assignments expire at their named tip (spec 4.1).

## Review

- Reviews 1-3 (GPT-6.2 Astra) of drafts 1-3: spec section 7.
- Review 4 of draft 4: spec section 7.
- [ ] The owner.

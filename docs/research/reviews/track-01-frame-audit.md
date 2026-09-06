# Track 01 frame audit — 2026-09-05

Author: Claude Code main session, at the owner's request, after the workflow
migration was accepted and all other sessions were stopped. Read-only: this
audit inspects existing records and retained sources. It collects no evidence,
runs nothing, and changes no investigation. It is a proposal for reviewed
returns, not a return itself.

## Why this audit exists

Two kill assumptions (`KA-05` timers, `KA-06` memory ordering) absorbed four
native campaigns and about a hundred review files without a disposition. On
inspection, neither question was one the project needed to ask:

- The timer bar (250 µs median at 1 ms) is failed by QEMU TCG, the product's
  reference, and by every VM on the platform. A bar the reference fails cannot
  measure "materially better than the reference".
- The memory-ordering question was answered by Apple in a document this project
  retrieved on 2026-09-03 and ledgered as `SRC-154`: Apple silicon has a
  per-thread `ACTLR.TSOEN` bit that switches the core to the x86-64 TSO model,
  settable by a kernel-privilege guest inside an Apple VM, and Apple states it
  exists for x86-64 emulators. The source was cited once, for a Rosetta footnote
  in `INV-01-02`, and never reached `INV-01-08`.

Both misses have the same root cause: every probe, prototype and campaign in
Track 01 was run as an unprivileged macOS process (families `F1`/`F2`/`F4`),
while the family that runs the emulator at EL1 inside a Hypervisor.framework VM
(`F3`) was retained as an "umbrella" and never touched. Conclusions about the
process families were then written as conclusions about the project.

## Screening questions applied

For each kill assumption and investigation:

1. **Shared?** Does QEMU TCG or an ordinary VM on this platform have the same
   problem? If yes, it is not a feasibility question for this project.
2. **Documented?** Does a retained or public platform document already answer
   it? If yes, the answer is a source citation, not a campaign.
3. **Which family?** Does the finding apply to the process families, the VM
   family, or both? A process-only obstacle does not kill the project.

Verdicts: **keep** (still decision-bearing as written), **close** (accepted
bounded finding suffices; no further work), **reframe** (the question is right
but the bar or scope is wrong), **stop** (planned or in-flight work should not
proceed as feasibility work; retain what exists).

## Kill-assumption register

| KA | Asks | Shared with reference/ordinary VMs | Documented answer | Applies to | Verdict |
| --- | --- | --- | --- | --- | --- |
| KA-01 | Process can get W^X JIT memory | Yes, QEMU uses `MAP_JIT` | Apple JIT docs | F2/F4 only; F3 owns its page tables | close; Developer ID/Hardened Runtime limb is a distribution source check |
| KA-02 | HVF runs x86 directly | n/a | Yes (no x86 VM API on arm64) | F5 | close (False, F5 killed) |
| KA-03 | 8 GiB guest RAM in one process | Yes, every VM app | Yes | all | close; drop the P1 replication as feasibility work |
| KA-04 | Faults in generated code catchable with full state | Yes, QEMU uses signals | Yes for signals; for F3 it is a native EL1 exception | F2 needs it; F3 gets it from hardware | close for F2; **stop** the M06-M1 delivered-Mach campaign (see INV-01-06) |
| KA-05 | Timers/wakeups bounded for guest tick and IPI | Yes, QEMU and every VM pay the same idle-wake latency | Yes: Arm Generic Timer, HVF virtual GIC (macOS 15) exposes the EL1 virtual timer as PPI 27 | absolute bar applies to nobody; F3 has a hardware tick | **reframe** (relative bar; guest requirement to Q-01-22) |
| KA-06 | x86-TSO preservable with public instructions | No, this is specific to x86-on-Arm emulators | Yes: Arm ARM ordering clauses for F1/F2; `ACTLR.TSOEN` via `SRC-154` for F3 | F1/F2 software; F3 hardware | **reframe** (spec argument for F1/F2; TSOEN probe for F3; retire litmus as proof) |
| KA-07 | Guest write tracking at 16 KiB host page or 4 KiB software | Yes, QEMU does software dirty tracking | Yes | process families; F3 picks its own stage-1 granule | close; F3 granule is one register read inside the VM |
| KA-08 | Device backings exist | Yes, every VM app | Yes | all | close; timer limb resolves with KA-05; vmnet needs the entitlement like every VM app |
| KA-09 | Toolchain exists and is sustainable | Yes, everything on macOS | Yes | all | close; profiling limb is "install Xcode" |
| KA-10 | No license incompatibility | n/a | Source audit accepted | all | close; residuals to the dependency decision |
| KA-11 | Translation, paging, exceptions, interrupts, 2 vCPUs compose | No, project-specific | No | held for F2; **never attempted for F3** | **keep**; the F3 composition probe is the only remaining feasibility experiment |
| KA-12 | A direct-boot guest machine contract for stock Linux exists | Yes: QEMU `microvm` and Firecracker direct-boot x86 Linux with virtio and no firmware | Yes, prior art | all | **reframe** to "adopt a proven x86 microvm contract"; design input, not feasibility |
| KA-13 | Containment budgets enforceable | Yes, every app | Yes | all | close; sizing to Q-05-02 |

Net: after reframing, eleven of thirteen are closed or design inputs. The two
that remain feasibility questions are `KA-11` for F3 and, conditionally, the
scope ruling on whether `ACTLR.TSOEN` is admissible (below).

## Investigations

Effort columns: `rev` is the number of historical review files mentioning the
investigation, `man` the number of evidence manifests.

| INV | rev | man | Verdict | Reason | Existing work |
| --- | ---: | ---: | --- | --- | --- |
| 01 platform APIs | 24 | 2 | close | accepted; no open feasibility limb | keep |
| 02 family register | 24 | 0 | **reframe** | register never records which family each finding applies to; F3's two documented hardware advantages (TSOEN, EL1 timer) absent | add a family column; add `SRC-154` TSOEN claim; make F3 the primary probe candidate |
| 03 address space | 16 | 2 | close | shared with every VM | keep |
| 04 JIT memory | 20 | 2 | close | F2-only, accepted | keep |
| 05 page tracking | 16 | 2 | close | F2-only, accepted | keep; F3 granule check joins the F3 probe |
| 06 fault delivery | 23 | 6 | close + **stop** | signal packet accepted for F2. The M06-M1 Mach-exception campaign (33 controls, 96 holds, 11,937 jobs planned, two failed attempts) explores an alternative delivery path for the process family; that is optimization, not feasibility, and F3 needs none of it | keep accepted packets; halt M06-M1 until a family decision |
| 07 timers | 55 | 8 | **reframe** | bar failed by the reference; measurements characterize idle-wake latency (~0.5 ms) shared by every design; busy-tick path never measured and not at risk | keep T2 packets as the baseline-path number; no further campaign; guest requirement to INV-01-22 |
| 08 memory ordering | 46 | 5 | **reframe** | correctness for F1/F2 is a spec argument (F08-WB1 already accepted; instruction audit is the right next step); F3 has `ACTLR.TSOEN` per `SRC-154`; litmus runs cannot prove absence | keep runs as harness validation and cost data; finish the instruction audit; no more litmus |
| 09 device backings | 13 | 2 | close | shared with every VM app | keep |
| 10 toolchain | 19 | 3 | close | shared with everything | keep |
| 11 licenses | 13 | 0 | close | accepted source audit | keep |
| 12 prototype | 22 | 2 | keep | F2 composition held; **F3 composition is the open feasibility question** | keep; F3 prototype needed |
| 13 machine contract | 7 | 0 | **reframe** | prior art (QEMU microvm, Firecracker) proves a direct-boot x86 virtio contract exists; remaining work is choosing one | design input for Track 02/03 |
| 14 resource budgets | 12 | 2 | close | shared with every app | keep |
| 18 code publication | 33 | 5 | **stop** | F2-only (`MAP_JIT` W^X protocol). QEMU has the same code-cache problem and solves it with safe points. F3 uses the documented Arm CMODX sequence at EL1. Planned expansion is 55,949 children and 1.68 million s of ceilings, exceeding host capacity | keep accepted bounded cells; halt expansion; design question for Track 03 if F2 survives |
| 19 JIT state across faults | 18 | 0 | **stop** | F2-only; composition of two F2-only mechanisms; zero runs; 1,000–5,000 children planned | correctness-track item for F2 only |
| 21 sleep/wake/App Nap | 8 | 0 | **stop** | shared with every VM app; handled by policy (pause on sleep, resync clocks) | product policy input to INV-01-22; halt M21-1 instrument |
| 22 guest time base | 3 | 0 | **keep** | the one source question that disposes the timer bar for every family | now also owns "what tick lateness does a tickless x86 Linux guest tolerate" |
| 23 scheduling/liveness | 3 | 0 | **stop** | shared with every VM on macOS; 8-vCPU VMs run on laptops today. Oversubscription behaviour is a performance-track measurement | halt M23-L1; move to Track 04 |

Effort distribution: the seven investigations marked reframe or stop account
for 191 of 224 historical review files. The three investigations that are
project-specific and still open (`12` for F3, `22`, `08`'s instruction audit)
account for 71.

## What the audit found that the record does not say

1. **`ACTLR.TSOEN` is documented and retained.** `SRC-154` (retrieved
   2026-09-03, SHA-256 in the source ledger) states that setting the bit
   "changes the CPU's memory model to the x86-64 TSO memory model", that Apple's
   Linux patch context-switches it per thread inside an Apple VM, and that the
   feature exists because "emulators that recompile x86_64 instructions to
   ARM64 instructions" previously "needed to emulate TSO, degrading
   performance". This is the single most decision-relevant sentence in the
   retained corpus and no investigation uses it.
2. **The HVF virtual GIC exposes the EL1 virtual timer in-guest.** The SDK
   header on the host of record (`hv_gic_types.h`, macOS 15.0+) defines
   `HV_GIC_INT_EL1_VIRTUAL_TIMER = 27`. With a virtual GIC, an EL1 payload
   takes its tick as a hardware interrupt without a host exit. Both required
   macOS releases are 15 or later. Not observed; header-level only.
3. **The reference emulator shares every "host obstacle".** QEMU TCG on macOS
   is a process using `MAP_JIT`, signals, software dirty tracking and
   user-space timers. Every process-family finding in Track 01 applies equally
   to the baseline. None of them can therefore establish or refute
   "materially better than QEMU TCG".
4. **F3 was never probed for a recorded reason.** No investigation records why
   the prototype is F2 rather than F3. `INV-01-12` says only that it "screens
   F3 in neither direction".

## Where the frame was lost — the 2026-09-02 reset

The owner's intended design was fully worked out before the reset. The
preserved checkpoint `a121702` holds 26 research documents and probes from
2026-08-08 to 2026-08-15 whose subject is exactly the hardware-assisted
design: a small EL1 payload inside a Hypervisor.framework VM, x86 shadow page
tables on real Arm stage-1 with the 4 KiB granule, hardware TSO via
`ACTLR_EL1`, hardware exceptions and timer, and a forked FEXCore backend
running as a host-process compile server. Its feasibility synthesis named this
"Tier 2" and made it the north star. Measured there, on an M2 Max under
macOS 26.5: 4 KiB stage-1 translation at guest EL1 passes; guest
`msr actlr_el1` and `HV_SYS_REG_ACTLR_EL1` both enable TSO inside the VM;
a full shadow fill costs about 146 ns (439 ns with TLB invalidation); the
shadow-fill path scales at 93–100 % parallel efficiency to 6 vCPUs; lazy
page-table resync driven by the guest's own `INVLPG`/`CR3` obligations is
architecturally sound; a QEMU TCG baseline was measured on the same host.

The reset design of 2026-09-02 (`docs/superpowers/specs/2026-09-02-research-reset-design.md`)
correctly demanded independent review, primary sources and reimplementation
before any of that could count as evidence. But it also listed as a non-goal
"selecting FEXCore, QEMU components, shadow paging, a helper VM, or any other
architecture in advance" and required feasibility to "compare multiple
candidate architectures without a preferred incumbent", inside an envelope of
"public macOS APIs". Under that mandate the new track built its prototype and
every probe on the easiest-to-instrument candidate, an ordinary process with a
software MMU, explicitly recorded that this "screens F3 in neither direction",
and never returned to the owner's design. The kill assumption on memory
ordering was then worded "with public instructions" because a native process
has no TSO API, ignoring the retained August finding that inside a VM it does.

The owner has now confirmed the intended design and the goal: use Apple
silicon's native page tables, interrupts, exceptions and memory model as much
as possible to reduce emulation cost. The owner's stated reason for the reset
stands: the August work was out of shape, tied to one laptop, and largely
unreviewed. The reset's rigor rules stay. Only its "no preferred incumbent"
clause is withdrawn. August code and results are not to be revived or reused;
the direction is kept, and every mechanism is re-implemented and re-measured
from scratch under the current workflow.

## Owner rulings — 2026-09-05

Both scope questions this audit raised were answered by the owner in session:

1. **Apple silicon hardware capabilities are admissible.** The project should
   use as many of them as possible. `ACTLR.TSOEN`, the EL1 Generic Timer, the
   HVF virtual GIC and any other documented platform control are in scope.
2. **No App Store or distribution constraint applies.** This is an experiment
   project. The hypervisor entitlement, Developer ID, notarization and Hardened
   Runtime limbs are not blockers; local signing on the owner's hosts suffices.

Consequences for the record:

- The Track 00 envelope line "public macOS APIs only" must be amended by a
  reviewed return to something like "public Apple platform capabilities,
  including documented hardware controls, with no distribution constraint".
  Until that return lands, this audit's family verdicts are conditional on it.
- `F3` (emulator at EL1 inside a Hypervisor.framework VM) is unblocked and is
  the primary candidate. `CLM-01-02-03` (entitlement for distributed apps) is
  moot for this project.
- Every "Developer ID / Hardened Runtime / notarized applicability untested"
  limb in `INV-01-01`, `-04`, `-18`, `-19` is closed as out of scope.
- `KA-10` licensing remains a hygiene item, not a kill: with no distribution,
  the compatibility table's distribution models are not decision-bearing.
- `KA-06` for `F3` is now a probe of a documented feature, not a scope debate.

## Proposed returns (reviewed, not silent)

1. `INV-01-07`: replace the absolute 250 µs falsifier with a relative one
   (no admissible family delivers guest tick/IPI at least as well as the
   reference on the same host); route the guest requirement to `INV-01-22`;
   retain T2 packets as the baseline-path characterization.
2. `INV-01-08`: record `SRC-154` TSOEN as the F3 disposition path; scope the
   software argument to F1/F2; retire litmus as correctness evidence; complete
   the instruction audit as the F1/F2 disposition.
3. `INV-01-02`: add a family-applicability column to the register; add the
   TSOEN and EL1-timer claims; record the two scope rulings above as blockers
   on F3; state that F2 findings do not transfer to F3 and vice versa.
4. `INV-01-06`, `-18`, `-19`, `-21`, `-23`: mark the planned or in-flight
   campaigns as suspended pending a family decision, with existing accepted
   cells retained. No evidence is withdrawn.
5. `INV-01-13`: reframe to contract adoption from prior art.
6. Workflow README, pre-execution review step: add the three screening
   questions above as mandatory items, and require every kill assumption and
   finding to name the family it applies to.
7. Track 00 scope: amend the envelope per the owner rulings above (hardware
   capabilities admissible; no distribution constraint).

## The one experiment that remains (deferred by owner instruction)

A single Hypervisor.framework probe with an EL1 payload: set up stage-1 tables
at the guest's chosen granule, set `ACTLR.TSOEN`, configure the virtual GIC and
arm the virtual timer, run the existing `INV-01-08` plain-access litmus and a
1 kHz tick on two vCPUs. It would dispose `KA-05`, `KA-06`, `KA-07`'s granule
premise and the core of `KA-11` for F3 in one bounded run, on both required
hosts. It is not authorized by this audit; the owner has deferred experiments.
It must be a fresh implementation under the current workflow, not a revival of
the August probes.

# INV-01-34 — pre-execution review

- Reviewer and independent context: `/root`, separate from the isolated Q34
  author `/root/track01_understanding_check`. The reviewer authored no Q34
  startup code, its standalone harness, or its source-method submission.
- Material inspected: current INV-01-34, `q34.c`/`q34.h`, Q31 context,
  mapping, exception and return helpers, Q33 continuation interfaces, the
  retained `q34-startup-preparation-01/preparation.json`, and the six exact
  Ubuntu/Debian trampoline, head64 and verify_cpu source inputs identified
  by the submission.
- Initial review: 2026-09-07, beginning 08:41 UTC. Verdict: **revise the
  integration method; continue preparation, no execution acceptance**.

## Screening

1. QEMU TCG and ordinary VMs also start secondary CPUs, but can use INIT/SIPI
   and real-mode execution. The question applies to the charter's exclusion
   of real-mode handling, not to macOS creating native vCPUs.
2. ACPI and the inspected Linux source already establish the mailbox entry
   contract. The accepted source finding should not be rediscovered by a
   mailbox-only experiment. The remaining experiment concerns realizing the
   actual mode transitions and handing their result to the existing shim
   mechanisms under SMP.
3. Applicability is the thin EL1 shim with the two inspected kernel revisions,
   finite CPU profile and startup paths. A source-level handwritten instruction
   fixture can test this mechanism without a general translator or stock-image
   boot. It does not establish general kernel compatibility or ISA correctness.

## Substantive return

**The current continuation does not consume the startup architectural context.**
`q34.c:continuation()` leaves the startup interpreter's final CR3, GPRs,
selectors and high private stack in `q34_arch`. It runs the Q31 fault example
in the unrelated low-address fixture, then XORs selected startup fields into
an ADD input. `q31.c:context()` restores its fixture's private root for mapping,
delivery and return. Startup memory occupies separate backing, so identical
numeric physical addresses do not refer to the same stored bytes.

Consequence: a successful arithmetic binding can coexist with an unusable
startup-to-runtime mapping or stack. It establishes neither that the started
AP can continue through the established paging mechanism nor that a fault or
interrupt can return to that state. The method correctly discloses this limit,
but disclosure alone does not fill the integrated-realization requirement.

Smallest remedy: make an explicit, traceable handoff of the final four-level
startup context and the memory it references into the existing Q30/Q31
execution path. Reuse the existing helpers; a general CPU model is unnecessary.
If separate backing remains useful during PG-off interpretation, record and
independently verify its import, preserve virtual-to-physical meaning and
the final architectural state, and prevent later helpers from silently
restoring the old fixture root. Any synthetic post-startup descriptor/handler
installation must be explicit and distinguished from stock kernel execution.

Closure: the independent oracle ties the final startup state and memory to
the first continued execution, observes native mapping/fault classification
and a Q33-produced instruction under that context, and checks fault/interrupt
return with the actual private stack. Negative controls must reject a wrong
imported root, lost stack/state, mismatched backing and a hidden reset to the
old fixture. The arithmetic binding may remain corroboration but cannot
substitute for these observations.

## Independent model-harness review — September 7, 09:38–09:40 UTC

Screening (a): a synthetic emulator harness shares startup-state and trace
provenance obligations; it cannot establish the native mechanisms ordinary
VMs supply. Screening (b): this harness uses ordinary host memory and modeled
notifications, with no new hardware capability claim. Screening (c): this
review covers only `/root`'s isolated `check_cases.c` integration harness at
`/private/tmp/natemu-q34-integration/experiments/track01`. Reviewer
`/root/track01_understanding_check` authored the earlier Q34 startup core but
not this harness. This is therefore no independent verdict on that core,
the integrated producer, or the whole Q34 method.

The Q34 model branch invokes the actual `q34_run()` and existing Q31/Q32/Q33
producer callbacks, rather than a completion stub. It deliberately seeds
earlier phase-complete state; those seeded counters are model admission inputs,
not evidence that the earlier suites ran. Q34 allocation/identity is present
but phase 0 for ordinary Q32/Q33 model builds, so newly shared hooks can read
dormant state. Active Q34 admits the modeled Q32 notification path. Diagnostic
acknowledgement is explicitly a synthetic observer checking command 1, current
serial and a declared diagnostic reason; native host observation remains an
independent execution obligation.

The harness records paired BEGIN/END requests and Q34-only setup and A/D
mutations with prior/new bytes. It emits initial and final runtime backing,
Q34 state, Q33 state/code pool and actual modeled host transfers. These model
records do not contain the native gate/ACK/fault protocol; format and printed
scope correctly say so. All output streams use fresh-file creation, and writes
and closure are checked. The model 03 outputs retain source hashes for their
earlier harness; they cannot certify the subsequent dormant-allocation change.

A separate strict `-O2 -Wall -Wextra -Werror` compile of the currently submitted
harness with the actual q31/q32/q33/q34/x86 sources passed. A fresh two-CPU
synthetic run passed 16,448 accesses with no stderr. Its trace contains all
16,448 paired BEGIN/END operations, 88,960 Q34 records and 960 host transfers;
initial/final memory are each 4 MiB, with separate Q33 state, Q34 state and 3 MiB
code-pool outputs. Exact source hashes, command, binary hash and outputs are
retained at `/private/tmp/q34-harness-review-0939-output`. This check exercises
the real producer through the model seam and makes no native execution claim.

Verdict: **the isolated harness is suitable for continued synthetic preparation;
no substantive harness defect found in this bounded pass.** Final preparation
must bind freshly rebuilt outputs to the frozen complete source/header inputs
and independent oracle. Native readiness, startup-core correctness and the
whole integration method require reviewers independent of their respective
authors. No source code was edited and no VM was run. This pass ended early
to prioritize the Q33 oracle return.

## Source-method assessment and limits

The Ubuntu compatibility32/PG-off/protected32/long64 sequence and Debian's
matched-LA57 alternative correspond to the inspected source branches. The
mailbox field widths, cache-before-command-clear sequence, actual 32-bit
lock operation with adjacent canary, private-stack-before-release check and
rotated publisher/contending AP roles address concrete startup obligations.
Keeping native arm64 translation enabled while interpreting the narrow
x86 PG-off interval is a legitimate candidate; it does not imply steady-state
five-level Q30 support.

The explicitly bounded `verify_cpu` callee contract excludes execution of
the general CPUID/vendor routine. The exact files show register and flag
clobbers and optional Intel MSR handling. The final oracle must distinguish
contract assumptions/unspecified clobbers from observed instruction semantics;
it must not accept a claim that the draft CALL marker executes that routine
or proves every final GPR equals stock execution. Its selected CPU-feature
profile and the subsequent source operations must justify which outputs
are used. No general CPU feature implementation is requested by this review.

The author reports strict compilation and two/four-thread startup-only
checks. They are preparation evidence, not independent VM replication, and
do not exercise the missing import, Q33 wrapper, diagnostic observer or
composite oracle. Exact integration code, adversarial controls, cumulative
resource/campaign budgets, full build and independent review remain work to
complete before execution. No editorial edits or implementation changes were
made by this reviewer.

## Returns

Keep subsequent responses and changed-material review in this thread. A
reviewer who authors a load-bearing integration change cannot independently
accept that change.

### Context-import preparation return — 2026-09-07 09:35 UTC

Reviewer `/root` inspected the integration authored by `/root/retained_runner`
in `/private/tmp/natemu-q34-integration`: Q34 project/import/runtime/continuation,
Q31 local context and fault/return helpers, Q33 continuation and native host
checkpoint/diagnostic paths. The original screening answers remain unchanged.
**The proposed integration now addresses the code-level continuity defect;
execution acceptance remains open pending exact oracle, controls and budgets.**

`project()` copies differing words from the full 128-KiB startup backing into
the same guest physical addresses through Q30's existing setup protocol and
checks every resulting word. `q31_import_context()` receives all 16 GPRs,
RIP/flags/CS/SS, retains CR2, and imports the actual CR3/WP/NXE/PGE; it sets the
local root/base/backing used by later mapping and frame helpers. The first
nonretiring `QI_IMPORT` probe reads the actual high private RSP without changing
the architectural state. It has an explicit diagnostic recipe, avoiding an
invented LOAD retirement at a source-level startup label.

After that probe, explicit fixture setup splits the imported high 2-MiB leaf
into identity-mapped 4-KiB pages under the same root and installs the runtime
descriptor/handler tables. CONTROL 12 and a source-level JMP mark this departure
from stock startup source. Actual encoded MOVs consume the imported stack,
load an immediate data address, incur a true absent-page fault, repair and
IRET, then reach a host-produced ADD and contiguous STI/NOP followed by timer
delivery/return. The final checks retain the startup private RSP, selectors,
root and ADD result. Q33 requests bind the outer Q34 serial/variant/repetition
and keep monotonic transaction identity. These are inspected code obligations,
not yet independently validated native observations.

**Independence disclosure:** `/root` subsequently authored the Q34 integration
of `check_cases.c`, including synthetic phase admission, diagnostic observer,
initial/final memory, setup/A-D provenance, state and transport outputs. Root
therefore cannot independently accept that harness. Another reviewer must
inspect it and its model results. Root authored no Q34 producer/context-import
code or independent oracle. Model 03 strictly compiled and completed 128 global
fixtures at two and four CPUs (16,448 and 45,960 modeled accesses, maximum 256
setup patches). Its exact source hashes and outputs are under
`/private/tmp/natemu-q34-integrated-model-03-{2,4}cpu`. The first model 01 failed
because root initialized the used pool-byte counter to its capacity; model 02
removed that harness mistake. Model 03 adds explicit setup/A-D records and
includes the producer's distinctive private-stack words. None ran a VM or
establishes native paging, interrupt, register-copy or stock-image execution.

Closure still requires the independent whole-memory/context replay and its
wrong-root/backing/stack/state/reset controls, startup branch/mode/source
contract checks, cumulative finite bounds, reviewed immutable build and
oracle, and bounded execution after Q33's runtime resolution. The 10:27:31 UTC
preparation deadline remains unchanged.

### Native checkpoint filename return — 09:49 UTC

Root's independent read of all four host checkpoint handlers found that
`q33_checkpoint_host()` still called `dump_file("q32-checkpoint.bin", ...)`.
The existing `O_EXCL` guard would refuse this second filename, retain the
earlier Q32 checkpoint and stop the run without a Q33 checkpoint. Strict
compilation and checkpoint-wait self-checks did not exercise this association.
This is a concrete execution/reproduction defect, not a platform finding.

The author reports build 01 was already retained at 09:48:33 UTC. Preserve it
as superseded compile-only preparation; no VM used it. The smallest remedy
is the correct `q33-checkpoint.bin` target and a source association check
covering each HVC, handler and unique checkpoint filename, followed by a fresh
build 02. The existing no-overwrite implementation needs no change. Exact fixed
inputs and a retained failing duplicate-target control must close this return
before Q34 execution review.


## Independent Q34 supervisor review — September 7, 10:20 UTC

Screening (a): bounded checker execution and source routing are ordinary VM/
emulator tooling obligations. Screening (b): no hardware discovery is involved.
Screening (c): this review covers only the root-authored Q34 supervisor
extension. Reviewer `/root/track01_understanding_check` authored the earlier
Q34 startup core but not this supervisor; no startup-core or whole-method
acceptance is inferred.

The exact candidate is `q34-startup-supervisor-01`: all 26 inventory entries
independently verified, inventory SHA256
`094d8d6d0b24d7c0e8c5ec2027ca2d54a23297642f2f4fe9abd338d8da8ecfe2`.
Its run_check.py SHA256 is
`09ded20296858f4ae4ec00fdb726e588da8b722cd9c3e0bf68c0b2bd2c90ca4a`.
The source diff from accepted Q33 supervisor 01 only adds mutually exclusive
`--q34`, selects check_q34.py, copies the seven-source Q30–Q34 oracle chain,
and chooses 12 GiB sampled process-group RSS. The 1800-second wall limit,
1-second sampling, 256 MiB checker output-file cap, fresh output allocation,
process-group termination and existing error paths are unchanged.

The retained generic supervisor self-check passes. The synthetic actual CLI
run selects Q34, retains all seven oracle sources plus the supervisor, and
records 12,884,901,888 bytes/1800 seconds/1-second sampling. Its copied supervisor
is byte-identical to the candidate; all nested output/oracle inventory entries
and separately pinned input inventory digests verified. The mock checker and
synthetic producer identity explicitly identify themselves as routing tests,
so their PASS is no startup or research-evidence claim. No additional full
oracle or native run was performed by this reviewer.

Verdict: **ACCEPT this exact supervisor implementation for Q34 preparation.**
The 12 GiB setting is a concrete enforced sampled limit, not a demonstrated Q34
working-set requirement. Final Q34 producer/oracle/resource and campaign
readiness must be reviewed separately by reviewers independent of those
load-bearing authors before execution. This review activates no campaign.


### Root combined producer/bound inspection — 10:25 UTC

Reviewer `/root` rehashed all 156 entries of `q34-startup-build-03`, inventory
`3dd18f27eaa0e773b73bd6e8cf61697957ac5d22853e64859090e5d180bc8edd`.
Only q32.c/q32.h/check_budget.py/check_cases.c differ from build 02. The native
checkpoint filename return is closed: build 02/03 use the distinct Q33 target,
and their retained source-association check rejects the duplicate-target
control. Superseded build 01 remains compile-only and has no native result.

The Q32 native source delta is the exact reviewed producer 05 bounded-trial
change, retaining Q34's existing phase admission and base hooks. No Q34 startup,
context import, continuation, assembly, host protocol or geometry changed.
The model merge preserves Q34-specific observer, memory/state provenance and
wrong-import-root control while adding producer 05's Q32-only controls. Root
inspected this merge but relies on a separate reviewer for the root-authored
harness. All 38 build/preparation commands passed, with a 229,376-byte guest;
Q34 synthetic results remain 16,448/45,368 operations at two/four CPUs.

The cumulative bound carries forward the inspected Q34 paths and adds the
independently reviewed 353,984-record Q32 trial allowance. It is 20,574,003 of
20,971,520 records, 397,517 spare. The Q34 terms cover 288 successful AP paths
at at most 128 steps; complete startup backing projection with a 1024 changed-
word guard;593 runtime table/fixture patches per global case;89,632 Q30 access
allowances including all 16 code-entry probes; and 64 epochs/32 role records
per global case. The cumulative pool ceiling remains 3248 objects/692,224 bytes.
Root checked the source assertions and arithmetic; observed model counts are
corroboration rather than the proof of the ceiling.

Verdict: **the submitted combined producer and source-path bound are suitable
for exact whole-method review.** No new substantive producer return was found.
This does not accept the unfinished independent Q34 oracle, resource campaign,
stock-kernel execution or a native feasibility result. Exact oracle/controls,
independent harness disposition and Q33's scoped resolution remain work before
Q34 execution. The source preparation completed within its original deadline.


### Retained root harness and finite campaign review — 10:30 UTC

Screening (a): complete model provenance and bounded resource accounting are
shared VM/emulator requirements. Screening (b): synthetic scheduling and
callbacks claim no additional native capability. Screening (c): the startup
interpreter remains the declared finite source-derived fixture, composed with
the earlier mechanisms only through the submitted integration seams. Reviewer
`/root/track01_understanding_check` authored the initial isolated startup core
and therefore does not independently accept that core or the whole producer.
This review covers root-authored harness/supervisor/resource work only.

All 156 entries of retained `q34-startup-build-03` independently verified,
inventory `3dd18f27eaa0e773b73bd6e8cf61697957ac5d22853e64859090e5d180bc8edd`.
The exact harness hash is
`d1fbe0baa45c180e03999bd058e1cd5f88715da68f005cced458a5d227aff887`.
It calls actual q34_run, retains initial/final 4 MiB backing, 135,168-byte Q34
state, 525,368-byte Q33 state, 3 MiB generated pool and exact host transfers.
Its Q34-only setup and A/D mutation events retain old/new data; every modeled
access has one matched BEGIN/END. Retained two/four-CPU traces independently
contain 16,448/45,368 matched access pairs. Synthetic diagnostics validate
current serial, command and declared reason before acknowledgment. Q34 state
is allocated but dormant in Q32/Q33-only builds; phase admission permits
actual Q32 callbacks during Q34. The imported-root negative changes the real
context seam and fails at QI_IMPORT with reason 13369, status 14. It does not
replace producer execution with a passing marker.

The integrated harness includes the already reviewed bounded-HLT injection
and partial failure output: eight early trials remain failure, with actual
records rather than a fabricated CASE END. Retained Q32 early0/1/7 two-CPU
and early7 four-CPU regressions pass 55,968/56,800/61,792/128,288 accesses;
Q33 two/four-CPU regressions pass 67,051/132,769. Earlier phase completion
fields are explicitly seeded for isolated model admission; these are not
earlier-phase execution evidence. The compiled callbacks and synthetic labels
cannot establish native ARM fault, gate, cache, IRQ or emitted-code execution.
**ACCEPT this exact harness for continued synthetic preparation**, with these
limits and the separate whole-producer/oracle reviews still required.

The proposed campaign is finite and internally consistent: primary 2/4 at most
3 VMs/4 checks/150 minutes; independent 2/4 at most 2/2/100 minutes; final
integrated 4 at most 1/1/50 minutes. New pairs require 45 minutes remaining and
same-RAM checks 35 minutes; first unexplained failure or resource/coverage
breach stops admission. Five 1984 MiB images plus 20,971,520*96 trace bytes equal
11.5625 GiB per run. Two required runs consume 23.125 GiB, so 28 GiB activation
reserve leaves 4.875 GiB; the optional third primary is explicitly not prepaid
and requires a fresh 14 GiB floor. That same 14 GiB floor covers the final single
run. All evidence stays retained. Each 1984 MiB image and 1920 MiB trace fits the
2 GiB producer file cap. Serial execution avoids competing full-memory VMs or
checkers. The 10/30/1800-second and 256 MiB checker-file limits remain unchanged.

**ACCEPT the sample, storage, time, slot and stop proposal as preparation.**
The 12 GiB RSS ceiling is correctly enforced by accepted supervisor 01, but its
working-set adequacy still requires the independently reviewed Q34 oracle's
buffer lifetimes and source-derived cumulative bound. Parent's reported
Q33 four-CPU peak 7,366,787,072 bytes is useful corroboration, not a proof that
all permitted Q34 traces fit. Q33 findings resolution, exact final inputs and
whole producer/oracle/budget acceptance remain prerequisites to activation.


### Campaign-wrapper substitution check — 10:33 UTC

Independent read-only comparison confirms the root-authored Q34 wrapper
`/private/tmp/natemu-q34-campaign-step.py` changes exactly two strings from
the retained Q33 campaign wrapper: campaign JSON path and checker flag
`--q34`. SHA256 `e9d18ff1cb2291792121146034e44ab69343bf09f131da9f02303d0e23b2637f`.
Admission deadlines, consumed slot accounting, fresh disk checks, retained
input-inventory pins, serial ownership and first-failure stops are unchanged.
This is an acceptable routing substitution for the reviewed candidate method;
final campaign JSON identities and the separate whole-execution readiness
review remain pending. The wrapper was not executed in this review.


Resource observation update — 10:36 UTC: the independently inspected terminal
Q33 four-CPU replica checker reports 8,681,504,768 bytes peak sampled
process-group RSS, larger than the primary's7,366,787,072. Use this larger
observed value when assessing Q34's proposed 12 GiB ceiling. Both remain finite
trace observations; the Q34 oracle buffer-lifetime and cumulative-bound review
is still required and is not replaced by subtraction from the limit.

## Independent whole-method return — September 7, refreshed review

Screening (a): ordinary x86 VMs and QEMU TCG can use INIT/SIPI, so the
question remains the thin shim's exclusion of real-mode handling. Screening
(b): ACPI/Linux source supplies the mailbox contract; the experiment tests
its realization and the actual architectural continuation, not its discovery.
Screening (c): this review applies to the declared finite Intel-profile,
source-indexed Ubuntu/Debian startup paths and Q30–Q33 shim composition on
the recorded M4. It cannot establish a stock-image boot or general decoder.
Reviewer `/root/q34_review` is separate from startup, integration, harness,
supervisor and oracle authors and authored none of those inputs.

The reviewer reread the workflow, charter, current investigation and this
thread; independently verified all 156 inventory entries of
`q34-startup-build-03` (inventory SHA256
`3dd18f27eaa0e773b73bd6e8cf61697957ac5d22853e64859090e5d180bc8edd`);
and inspected the complete startup code, six retained source inputs,
context-import/fixture helpers, generated continuation, host checkpoint and
device observer, native hook admission and source-derived record/pool bound.
Ubuntu's compat32/PG-off/protected32 path and Debian's LA57-matched PUSH/LRET
path preserve PE and agree with the chosen source branches. The concrete
projection/import consumes the final CR3 and private-stack backing, and the
continued fault, generated ADD and timer paths use that root and stack.
The declared `verify_cpu` contract remains an assumption for the successful
callee/profile, not execution of its CPUID/MSR body. The selected source path
ends at private-stack installation/lock release; subsequent descriptor and
handler installation is explicitly fixture work.

**Return: the native host-device service is inactive during Q34.** In the
sealed `host.c:device_worker`, the branch servicing Q32 SPI assertion,
deassertion and diagnostics requires `interrupts->phase == 1 || code->phase
== 1`. Both phases are 2 after the Q33 checkpoint and during Q34. In contrast,
`q32_irq` and `q32_hook` explicitly admit Q34 phase 1. Q34's selected timer
continuations do not need the host-device worker, so their success could hide
this gap. It conflicts with the charter's requirement that every earlier
mechanism remain on in the growing program.

Smallest remedy: admit `startup->phase == 1` to the existing host-device
branch, retaining its existing SPI/stopped-owner predicates. A focused source
or synthetic admission control must distinguish the Q34-only active state
from a fully inactive state, then freeze a fresh producer build. No new device
model or additional workload is required. Closure requires independent review
of that exact change and its control; build 03 remains immutable preparation.

Verdict: **revise for this growth-rule return; no VM execution acceptance.**
Exact independent oracle/controls, buffer-lifetime/resource acceptance and
campaign activation inputs remain separately outstanding. No VM was run, no
load-bearing implementation was changed, and archived history was not read.

The same independent reviewer inspected the concrete inactive campaign draft
`/private/tmp/natemu-q34-campaign-prepared.json` and the campaign wrapper.
The declared 2/4-CPU order, 150-minute/3-VM/4-check primary caps, conditional
single diagnostic slot, 45/35-minute admission reservations and 28/14 GiB storage
floors agree with the previously reviewed preparation proposal. The wrapper
pins producer/oracle/supervisor inventories, admits fresh identities, charges
slots before subprocess execution and records first-failure stops. Its inputs
must be repinned to the corrected producer and exact oracle before activation;
null oracle identity and `prepared_not_active` remain work status.

Resource review must distinguish the source record ceiling from checker RSS:
`q30.decode` materializes cumulative records as Python tuples and integers,
and paging/metadata replay adds indices. The 20,574,003-record arithmetic alone
does not prove these structures fit 12 GiB. Review the final oracle's actual
buffer lifetimes; observed Q33 peaks cannot substitute for that assessment.
A guarded attempted-run ceiling may retain a resource-limited result, but must
not be described as proven working-set adequacy or a feasibility PASS.

### Host-device growth-rule return closed — September 7, 21:07 UTC

The same independent reviewer rehashed all 156 entries of corrected
`q34-startup-build-04`, inventory SHA256
`7d35e16b03f10eac92ca9cc19ec313d7111b98ed51fabf4df79049f04b8442c0`,
and compared every producer source against build 03. The only producer delta
adds `startup->phase == 1` with OR to the existing shared host-device admission
predicate. The SPI/stopped-owner/diagnostic body is unchanged. Thus the route
now remains active in the Q34-only state while the fully inactive state stays
excluded. No guest source, emitted image, record bound or pool bound changes.

All 38 retained build/preparation commands pass. The retained source-association
check now verifies all three phase inputs in the actual device-worker branch
and rejects the exact original omission. Independent inspection verifies the
OR semantics that its source text test alone would not prove. The prior
checkpoint filename control remains present. **The specific growth-rule
return is closed; accept corrected producer 04 for the declared method.**
Whole-execution acceptance still awaits the exact native composite oracle,
its adversarial controls and buffer/resource assessment. No VM was run by
this reviewer, and build 03 remains an immutable superseded preparation.

### Independent replication build preparation review — September 7

Reviewer `/root/q34_review` independently inspected the fresh rebuild prepared
by `/root/findings_review`, who authored none of the Q34 producer or oracle.
The 156-entry inventory of `q34-startup-replica-build-01` rehashes to
`990d5510deb3c304a3541a95c7e0b725e6a038ac6631b26fb8ec4cc0bcbaca18`.
All 27 retained source inputs match producer 04, including the six vendor files.
The manifest records independent strict compilation of each guest source and
the host from the replica's own source directory, fresh linking/signing and
all 38 commands passed. Preparation helpers are copied from sealed producer 04
rather than mutable temporary files; that helper-directory change is the
only preparation-driver delta and changes no check semantics.

**Accept this independent rebuild/fixture preparation.** Its separately
produced synthetic traces remain model evidence; they do not become native
replication. The final exact oracle must check those independent traces and
its preparation controls must be reviewed before the separately bounded,
serial native replica campaign can activate. No additional VM slot or campaign
activation is authorized by this build-only review.

### Independent adversarial harness review — September 7

Reviewer `/root/q34_review` inspected the complete harness authored by
`/root/findings_review`, separate from the oracle author. The helper
`/private/tmp/q34-independent-negative-controls.py` has SHA256
`721a1fad1ce71ebdc7250b9559e8b7622609cf77aafec363b39609a94acbdb23`.
It first accepts the complete 128-case two/four-CPU models and the isolated
positive paths it later corrupts. Deep copies preserve shared operation/state
references inside each mutation while separating it from later controls;
only the oracle's `ValueError` counts as rejection, so incidental exceptions
cannot create passing controls.

The 29/31 rejected changes cover skipped/mis-sized FAR/LRET and segment/CR
operations, incorrect mode/PE state, cache-before-ACK, lock canary, wrong root,
backing/private-stack/GPR reset, a fabricated retiring import, generated result
and stack disconnect, changed final timer-return flags and absent native-fault
witness. Four-CPU controls additionally remove the failed BTS observation and
move publisher activity outside its required overlap. Five final controls
remove, duplicate or misbind the host diagnostic case/vector/command using
explicitly synthetic positive host rows. Those rows are never native evidence.
The inspected temporary two/four-CPU outputs each report 128 positive cases,
29/31 precise rejections and empty stderr.

**Accept this exact adversarial harness implementation.** Its final execution
must be retained against the exact frozen oracle and input identities before
whole-execution acceptance. This review neither supplies native observations
nor accepts an evolving oracle by association with passing model controls.

### Final integrated campaign preparation review — September 7

The same independent reviewer inspected
`/private/tmp/natemu-q34-final-campaign-prepared.json`:50 minutes, one VM and
one full check at four CPUs, no diagnostic retry, 14 GiB activation/new-run
floors, 45/35-minute admission reservations and first-failure stop. The final
wrapper differs from the reviewed primary wrapper only in its campaign JSON
path. **Accept this concrete final-run budget and routing preparation.**
It remains inactive until Q34 findings and independent 2/4-CPU replication are
accepted and the same final exact inputs are pinned. A failed final run still
requires disposition; earlier successes cannot silently replace this gate.

### Composite-oracle backward-compatibility return — September 7

Reviewer `/root/q34_review` verified the frozen six-source regression chain
`/private/tmp/q34-q33-regression-oracle-01`, inventory
`c42fb549112e3fdb0b8cb7a17f1c566da93b3b7ea738e9637daa5b768501a27e`,
and inspected its full delta from accepted Q33 oracle 04. Optional continuation
bases/instructions, complete-then-filtered architectural snapshots, filtered
metadata projections, explicit v6 geometry and lazy v6 record access preserve
the earlier method in the inspected default paths, except for this return:

**Terminal Q33 silently drops orphan host-production records.** The new
`check_q33.check` constructs `eligible` from actual transfer keys, then collects
`q33_produced` rows only when their keys are eligible even with
`snapshot_only=False`. Previously every production row was collected and the
consumed-production/set equality rejected orphan output. A production row with
no transfer now disappears before that equality, weakening the earlier oracle.
Q34's separate prefix/continuation filters can omit the same orphan globally.

Smallest remedy: preserve unconditional production-row collection in terminal
Q33 checks, limiting eligibility filtering to the explicit checkpoint mode;
require cumulative Q34 production rows to have a corresponding actual
Q33/Q34 transfer. Retain a focused orphan-production corruption control, then
freeze fresh exact inputs. Positive same-RAM regression alone cannot reject
this counterexample. **Return for this correction before the proposed native
same-RAM regression checks; no prior positive observation is contradicted.**

### Exact same-RAM regression readiness — September 7

The reviewer independently rehashed regression chain 02, inventory
`ac4ebe7eaea5c01e8aecbad2c21b81f7cf47a00d1852ee5e08848c42430791a2`.
Its only checker delta from 01 restores unconditional terminal-Q33 production
collection; eligibility filtering is confined to explicit snapshot mode.
The terminal orphan-production return is thereby closed at the source level.
Chain 01 remains an unexecuted rejected preparation. Q34's cumulative transcript
partition/negative controls remain final-oracle review work.

**Accept this exact chain for two bounded same-RAM regression checks** of
`q33-code-replica-01` and `q33-code-replica-02`, using accepted
`q33-code-supervisor-01` with 10 GiB sampled RSS/1800 seconds per check. The
campaign has 90 minutes from explicit activation, at most two checks, serial
execution and first-failure stop; it creates no VM or new mechanism observation.
A passing regression establishes compatibility with these retained native
traces, not Q34 success or guaranteed Q34 working-set adequacy.

## Exact whole-method execution acceptance — September 7

Reviewer `/root/q34_review` retains the independent context recorded above and
authored none of the producer, oracle, model harness, adversarial harness or
supervisor. The original three screening answers and finite source-profile
scope remain unchanged. **ACCEPT the complete declared Q34 method and these
exact inputs for the bounded primary campaign. No substantive return remains.**

- Producer `q34-startup-build-04`:156 entries, inventory
  `7d35e16b03f10eac92ca9cc19ec313d7111b98ed51fabf4df79049f04b8442c0`.
- Oracle `q34-startup-oracle-01`:58 entries independently rehashed, inventory
  `643d01c2b42d968feb27a2d3bc736f59c5751a061847b7f81e2bd0b3eab924c3`.
- Supervisor `q34-startup-supervisor-01`: independently reverified inventory
  `094d8d6d0b24d7c0e8c5ec2027ca2d54a23297642f2f4fe9abd338d8da8ecfe2`.

The final oracle independently reconstructs source operation/mode/register
semantics, startup table walks and actual memory effects, exact mailbox target
order and field widths, cache-before-ACK, boot-once behavior, lock ownership
and private-stack release. It requires actual failed BTS contention and the
publisher's paging/load/native-timer/guest-return interval while the first AP
holds the lock. Cold contexts require ignored-AP observations plus an exact
host diagnostic record with command 1; an AP ACK is not substituted for that
external termination.

The native adapter verifies the untouched Q30–Q33 same-VM checkpoint chain,
full native backing projection and its actual setup-mutation count, full
architectural import, high private-stack GPA/value, source opcode bytes,
native page-fault classification, original-stack fault frame/IRET/retry,
actual register-transported host-produced ADD and native timer/guest-return
preserving the continued state. Earlier guest paging/code/IRQ counters and
completion identities remain intact. Q33 transport/object semantics retain
source generation, actual native template/entry/effect PC, immutable pool and
canonical-result checks. Every host production belongs to an actual transfer,
and every transfer belongs to the complete Q33/Q34 partition. Wrong-root,
backing, stack, GPR, hidden reset, fabricated result and diagnostic controls
close the original disconnected-continuation return.

All 18 sealed preparation commands passed with empty stderr: the exact 2/4-CPU
models each cover 128 cases, the separately authored controls reject 29/31
corruptions, all earlier oracle self-checks and Q32/Q33 model regressions pass,
and the real eight-early-trial partial output is correctly rejected after
complete service. The compiled native-helper decoder passes. These remain
synthetic/compile evidence. The reviewer inspected the actual controls and
results, not just their PASS labels.

Both independently retained native Q33 same-RAM regressions also pass with
this exact six-source dependency chain: `q34-q33-regression-check-01/02`
cover 2,210,834/4,590,876 records and 177,388/359,499 operations, all 448 Q33
cases, with no unsupported replay. Their checker inventories and terminal
outputs were independently inspected; observed RSS was 7,462,305,792 and
8,350,744,576 bytes, within the separate 10 GiB/1800-second guards. This preserves
prior native semantics without claiming a Q34 native result.

### Resource and activation disposition

Accept the retained `MEMORY.md` argument as justification for a **bounded
attempt**, not guaranteed fit for every permissive trace. The producer's
20,574,003-record/523,136-native-operation ceilings and 3248-object/692,224-byte
pool bound remain finite. V6 uses a read-only lazy record view; complete
snapshot chunks are validated before unused prefix/intermediate state objects
are discarded. Inspecting the consumer paths confirms no semantic obligation
requires the discarded chunk objects. Nested predecessor maps are released
before their caller's full replay, and final-image equality runs after nested
continuation checks. At most two complete image mappings are scanned together;
Q34 closes the Q33 mapping before final replay. The full-capacity view control
scanned 20,971,520 records with a 48-byte view and 2,038,317,056-byte process peak;
that observation covers record storage only.

The enforced 12 GiB sampled RSS/1800-second checker ceiling, 256 MiB output-file
cap, existing producer timeouts and 2 GiB file cap, serial ownership and retained
first-failure stop remain requirements. A resource-limit outcome is a retained
failure and cannot supply a correctness PASS or trigger an automatic enlarged
campaign. The 150-minute primary campaign remains at most 3 VMs/4 checks with
required 2/4-CPU runs, 28 GiB activation and 14 GiB per-new-VM admission, 45/35-minute
remaining-time reservations, and only the already-declared conditional slots.
Q32 reclosure and Q33 scoped findings are now accepted in their owners.
Root may activate this exact primary campaign after fresh storage/host/input
checks and setting its start/deadline once. This accepts execution readiness;
Q34 findings, independent native replication and final integrated/breadth
acceptance remain required for Track 01 closure.

### Exact independent native replication readiness — September 7

The same reviewer rehashed all 92 entries of
`q34-startup-replica-prep-01`, inventory
`8c718619cf893586ec0eb97c954bdc425693a5dfac19148104e3042121257b60`.
All 15 commands passed with empty stderr. The preparation copies the exact
accepted oracle/supervisor, verifies 27 source inputs and 13 rebuilt artifacts
against producer 04, and applies the oracle to the independent replica's own
2/4-CPU synthetic traces, including 128 Q34 cases and 29/31 rejection controls.
Earlier Q32/Q33 models, eight-early-trial rejection, all self-checks and the
compiled native-helper check also pass. These are preparation observations,
not native replication.

The independently inspected activation script has SHA256
`30228e5172fb644bc37a0bca474e7572446784e7f5cde4691085b6112b59da06`.
It revalidates pinned input inventories, admits 28 GiB initially and 14 GiB per
new VM, requires 45 minutes remaining before each pair, executes the exact
2/4-CPU VM/check pairs serially, retains commands/outcomes and stops at the
first failure. The total budget is 100 minutes, at most 2 VMs/2 checks, with
accepted 12 GiB/1800-second checker guards and no extra retry. **Accept this
exact independent replication preparation and finite campaign**, subject to
root's explicit serial-runtime handoff after the primary campaign. The
replicator remains separate from producer/oracle authors and primary executor.

### Watchdog remedy after retained incomplete primary — September 7

The independent reviewer inspected `q34-startup-02` and the retained
`q34-startup-watchdog-diagnosis-01` read-only control, as recorded in the
findings thread. The control accepts complete primary 01 and rejects incomplete
primary 02; it does not substitute for the full oracle. Its source and retained
outputs match the separately inspected raw failure: completed case 101, next
initialization in progress, no recorded semantic failure or held startup lock.

**Accept exact producer 05 for the original remaining four-CPU primary slot.**
All 156 inventory entries independently rehash to
`578999408c5be7393a2f913ff388f6e1c832b2b7a46f08e2bf35c173bb51edc2`.
All 38 preparation commands pass. The only producer-source delta from 04 is
`deadline.tv_sec += 10` becoming `+= 20` in the existing host watchdog;
all guest objects, executable and image are byte-identical. The fixed 20-second
observation window remains within the unchanged 30-second outer producer child,
with no guest semantics, oracle, record/pool or per-file budget delta. The
accepted oracle 01 and supervisor 01 remain unchanged.

This is a coverage remedy, not acceptance of the failed run or a progress-rate
claim. Primary 01 retains its 04 identity. The original 150-minute campaign still
ends 23:59:24 UTC, admits no more than 3 VMs/4 checks, and has only one VM slot
left. Revalidate exact inputs and the 14 GiB/new-VM/45-minute admission floors
before primary 03. Any further failure stops that campaign. The independent
replica and final runs must carry the accepted 05 host watchdog source with
their own reviewed, retained inputs; neither budget is renewed by this return.

### Refreshed replica preparation for producer 05

**Accept the refreshed independent replication preparation.** The reviewer
rehashed all 156 entries of `q34-startup-replica-build-02`, inventory
`e83dbc6edbb8116595fe3c20430b66b29c6ebd77588db0871ff0072d17d4132f`,
and verified its sources and compiled outputs equal accepted producer 05. Its
38 build/preparation commands pass. The reviewer also rehashed all 92 entries
of `q34-startup-replica-prep-02`, inventory
`643d630e426c52e05f68c3fac107b90d7257c7171cb5fd3b2f55406986bd1a92`;
all 15 exact-oracle preparation commands pass, including the independently
rebuilt models, semantic controls and inherited regressions.

Relative to the previously accepted preparation, changes only update the
producer/build/preparation identities and record the 20-second host watchdog.
Activation script 02 has SHA256
`f396995ae166780b7c2582f39dfbae88aca36428da4dddcf967bcc5cb9625cdf`;
its only changes from accepted 01 select the refreshed preparation and new
activation output identity. The 100-minute/2-VM/2-check campaign and admission,
resource and first-failure guards are unchanged. It remains inactive until
root supplies the serial handoff after a passing primary four-CPU full check.

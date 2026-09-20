# INV-03-02 — Preparation and pre-execution reviews

## Wrapper source-preparation review — translator, September20

**Scope:** root-authored `experiments/track03/x86-semantics/run.py` and the reused,
unchanged `experiments/track01/source/run.py` retention functions. Source inspection
only. Probe and README were not present during this review and are not approved
by it. No build, helper self-check, wrapper or probe was executed. Whole-method
and execution acceptance remain pending; the stated45-second/six-case run is a
proposed budget, not an authorization.

**Verdict:** wrapper source preparation is suitable in the examined scope; no
substantive return. Removed only the unused `sys` import. It reuses the existing
retention implementation instead of introducing another supervision framework.

The wrapper allocates a fresh external raw directory through the existing
name/path/no-overwrite guards. It copies probe, README, itself and the helper,
checks capture equality and hashes the retained inputs, then loads the retained
helper for subsequent commands. Source-only mode returns before any build or
probe. On x86_64 Linux the default builds with strict flags and retains compiler,
host/proc metadata and disassembly; only explicit `--execute` reaches one probe
invocation. Missing files, compiler or objdump fail without attempting a probe.
This wrapper does not establish the eventual probe's case coverage or oracle.

The helper records command arguments, cwd, output paths and CPU limit, starts a
new process session, retains separate exclusive-create stdout/stderr, and waits
with finite timeouts. Its finally path kills the command process group even if
the leader already exited and reaps the leader. The probe's45-second call inherits
RLIMIT_CPU (`cpu_limit=False`), which is recorded rather than falsely described
as unlimited; the inherited helper also disables core dumps and bounds output
file size. Probe descendants must remain in the supervised group; the eventual
probe review must check that condition and its own ptrace-child cleanup.

Caught failures and interrupts after capture setup retain status/error and finish
time; final manifest precedes checksums, avoiding a self-hash cycle. A killed
wrapper or OS/storage failure cannot be claimed to produce a completed final
manifest, and a retained partial run must never be relabeled successful. The
helper's pre-command manifests preserve command intent if a later failure occurs.
No automatic retry, hidden execution after build-only, production change or
approval-receipt gate is introduced. Runtime rows remain evidence requiring
separate findings review, not success inferred from exit status alone.

## Return 2 — ptrace producer and six-case method source preparation

**Reviewer:** memory, independent of machine's producer/README and root's wrapper.
**Material:** `experiments/track03/x86-semantics/probe.c` and `README.md`, with
wrapper supervision scope from the preceding independent review and the Linux
v6.12 observer source chain in findings Return46. Source inspection only: no
Linux compile, disassembly, producer invocation or native observation. A source
preparation verdict is not host-specific pre-execution acceptance or a run budget.

The fixed assembly bytes and label arithmetic distinguish the intended debug
stop before the CC marker from a fallback marker trap after it. Successful ENTER
controls instead expect the post-CC RIP. Fresh children have a valid copied SS
selector, one two-byte read/write watchpoint only where required, inspected
initial data, cleared TF/RF and disabled syscall-restart metadata. Ptrace arming
and context readbacks are checked before CONT; no SINGLESTEP or child handler
intervenes. The watched NOP and plain ICEBP controls constrain B0/other B/BS
interpretation without forcing the disputed combined B0 result.

The nesting0 ENTER fixture puts its initial push wholly in the writable second
page and its final eight-byte extent wholly in the protected first page. Its
old-BP value is data, not a dereferenced noncanonical frame chain. The distinct
sentinel is read from the child before execution and the same slot is inspected
at the first signal-delivery stop, with old BP/SP, fault RIP and fault address
checked. Writable-final-page and allocation0 controls verify successful push and
register outcomes. The faulting case does not demand either sentinel or old BP
as its answer; a different word remains raw evidence for findings review. A
control after the disputed case can invalidate the invocation as a whole; no
standalone disputed line constitutes decision evidence before all controls pass.

Child setup prefaults the instruction/selector and resolves the frame COW before
TRACEME; this is not page pinning or elimination of all kernel interference.
PDEATHSIG includes its parent-race check, EXITKILL is required, and children do
not leave the wrapper's process group. Every continued case is bounded, and
cleanup kills rather than resuming the fault. Unexpected exits/API errors or a
cleanup failure remain failures. The outer timeout can cut short the sequential
case ceilings; only all six terminal observations with valid controls qualify
as a completed method invocation. No timeout or partial output is promoted.

**Narrow documentation return:** distinguish the unmodified PEEKUSER return from
raw hardware DR6: Linux reconstructs logical ptrace causes. Require actual-kernel
observer/notifier applicability review and Intel errata/provenance review before
execution; parent CPUID does not prove which physical core executed the child.
These are limitations of interpretation, not reasons to invent a kernel module,
privilege change, CPU pinning framework or synthetic Linux headers.

**Recheck/final verdict:** the README now explicitly records reconstructed DR6,
the actual-kernel debug/notifier check and uncontrolled child placement; the
narrow documentation return is closed. Accept producer/method **source
preparation only**. No substantive source-preparation return remains. A real
Linux build/disassembly, host/kernel/erratum applicability assessment, reviewed
retained inputs and a separately granted finite execution budget remain required
before any decision-evidence run. No numeric/ENTER/debug outcome is accepted by
this review. `git diff --check` passed; it is not a C build check.

## Return 3 — explicit AMD/KVM exploration admission change

**Reviewer:** memory, independent of machine's change. **Verdict:** accept the
bounded source change for explicitly labeled exploration; no substantive return.
Compared current probe/wrapper against retained source-preparation01 inputs and
read the amended README. No producer, build or remote command was executed by
this reviewer.

The C diff changes only main's strict optional argument, purpose/schema output,
and admission of non-Intel or advertised-hypervisor contexts when `--explore` is
present. Unknown/extra arguments fail. The six instruction fixtures, arming,
first-stop assertions, disputed raw observations, fail-on-control-error behavior
and cleanup are unchanged. Default admission remains guarded. The wrapper's
explicit flag forwards to the sole probe invocation; source-only and default
build modes do not acquire implicit execution. Both formats become v2 and record
exploratory purpose, applicability mode and flag state. Retention,45-second
supervision, inherited CPU limit, child count and no-retry scope are unchanged.

AMD/KVM results are observations of that mediated context. Even valid controls
cannot establish native Intel behavior or close51/55; invalid controls stop and
remain exploratory failures rather than motivating an automatic weaker oracle.
The README states these limits. This review does not itself grant remote access
or an execution budget: root's separately authorized bounded exploration governs
those actions. Earlier decision-evidence host/kernel requirements remain for any
future proposed promotion; these exploratory results cannot be relabeled into
accepted decision evidence.

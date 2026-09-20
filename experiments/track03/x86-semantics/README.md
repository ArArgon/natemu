# Native Intel first-stop semantics preparation

Status: **built and run once as an AMD/KVM exploratory comparison**. All six
method/control cases completed; this establishes no Intel semantic outcome.
See INV-03-02's owner-authorized AMD/KVM comparison and retained `amd-explore-01`.
The one-run comparison budget is consumed. No native run budget has
been granted for a native-Intel decision-evidence run. The user has authorized
using the available Coder AMD/KVM machine for bounded exploration; root owns its
immutable transfer, build and any execution. An independent producer/method review and explicit run budget are
required before execution. `run.py` owns retained source snapshots, real Linux
compilation, host inventory, disassembly, process-group timeout and raw outputs;
this directory adds no second runner. Do not fake Linux headers on macOS.

This addresses exactly INV-03-02 claims51 and55: the logical DR6 result of a data
watchpoint on MOV SS followed by ICEBP, and whether ENTER's earlier ordinary-RAM
frame store survives a later final-SP page fault. It is an observation of the
named implementation, not proof of every architecturally permitted outcome.
AMD/KVM observations cannot resolve either Intel-specific architectural question.
The producer does not declare either disputed observation an architectural PASS.

## Applicability and finite budget

Use genuine Linux x86-64 and its native compiler/libc/ptrace headers, 4096-byte
pages, and a native Intel host. The producer logs CPUID vendor, family, model,
stepping and hypervisor bit and rejects non-Intel or an advertised hypervisor
before starting children by default. The explicit `--explore` option alone
bypasses that vendor/hypervisor guard; it does not relax any case, stop/control,
page-size, cleanup or resource requirement. All invocations record
`purpose=exploratory`; the manifest also records the explicit mode. A clear hypervisor bit is not proof that virtualization
is absent: retained host provenance must independently establish the native claim.
Kernel ptrace mediation and known CPU errata (including the recorded MOV-SS debug
erratum) remain limitations. PTRACE_PEEKUSER DR6 is Linux's virtual/reconstructed
logical-cause report, not untouched hardware DR6; “raw DR6” below means the raw
API return. The INV-03-02 debug-oracle method records the Linuxv6.12 traps,
hw_breakpoint and ptrace path. Before a native-Intel decision-evidence run budget, review the actual host
kernel's debug/notifier path against that applicability premise. An exploratory
AMD/KVM result remains mediated by its actual kernel and hypervisor; missing Intel
applicability is not silently treated as satisfied. Parent CPUID
does not identify the core on which each child executes: scheduling is
uncontrolled. Host topology and applicable per-model/stepping errata must cover
all eligible child CPUs, or the native claim remains inconclusive. Do not infer
an architecture guarantee from one host.

One invocation, six fresh sequential children, no retries, no privilege changes,
no external process attachment or external data. Each child inherits the ordinary process mappings and descriptors; its test uses
one fresh three-page anonymous mapping. Private COW test memory is inspected with
ptrace.
The parent polls each initial/observation stop for at most3 seconds, kills and
reaps each child with a2-second deadline, and stops on the first invalid control
or method error. The wrapper's45-second process-group timeout is the outer bound.
No child leaves that group. `PTRACE_O_EXITKILL` and child `PR_SET_PDEATHSIG` are
mandatory and checked. A parent-death race check accompanies PDEATHSIG. No signal
handler is installed; the first observation stop is read before any signal is
delivered to guest code. Cleanup sends SIGKILL, never continues the faulting
instruction. Unexpected ptrace errors, exits, timeouts, RIPs, or missing controls
are retained failures, not a reason to widen the method.

## Exact instruction fixtures

The parent replaces RIP/RSP/RBP/RDI at the initial child SIGSTOP, sets
`orig_rax=-1`, clears TF and RF, and reads back the context. It reads back DR0/DR6/
DR7 and initial child memory. DR7 bit10 is ignored only in the arming comparison
because it is reserved fixed-one; the raw value is still logged. No inline C,
logging, stack calls or instrumentation executes between fixture instructions.
Global assembly labels delimit the byte sequences; disassembly must confirm them.
Before TRACEME/initial SIGSTOP, each child reads the8-byte entry span and selector
through volatile accesses and rewrites the frame sentinel to resolve its COW page.
The deliberately protected final-check page is not touched. This avoids a known
uncontrolled initial code/selector/COW fault inside the watched sequence; it does
not pin pages or rule out every later kernel memory-management event.

| Case | Exact bytes before marker | Armed breakpoint | First-stop control |
| --- | --- | --- | --- |
| `plain_f1` | `f1` | disabled | SIGTRAP, RIP at `plain_after`, B0–B3=0, BS=0 |
| `watch_movss_nop` | `8e 17 90` (`mov ss,[rdi]; nop`) | DR0 points to2-byte valid SS selector; local DR0, RW=read/write, LEN=2 | SIGTRAP, RIP at `watch_after`, B0=1, B1–B3=0, BS=0 |
| `watch_movss_f1` | `8e 17 f1` | identical | SIGTRAP, RIP at `fused_after`; B1–B3=0, BS=0; raw DR6/logical B0 is the disputed observation |
| `enter_final_absent` | `c8 00 10 00` (`enter4096,0`) | disabled | SIGSEGV at ENTER, fault address=final SP, old RSP/RBP; frame value is disputed |
| `enter_final_writable` | same | disabled | SIGTRAP after marker; expected new RBP/RSP and saved old RBP |
| `enter_alloc_zero` | `c8 00 00 00` | disabled | SIGTRAP after marker; expected new RBP/RSP and saved old RBP |

Each marker is one `cc` INT3 byte. For debug fixtures, the expected trap is
**before** marker execution, so RIP at the marker distinguishes it from a
fallback marker trap (RIP one byte later). For successful ENTER controls the
marker executes, so expected RIP is one byte after the marker. The SS selector
is read from the native parent and copied into the child fixture, then verified
before CONT; it is not a guessed selector. TF=0 eliminates single-step ambiguity.
Raw DR6 is retained; reserved bits are not treated as logical breakpoint causes.

For each ENTER case, a fresh12288-byte mapping has initial RSP=`base+8192`, frame
word=`base+8184`, old RBP=`0x1122334455667788`, and the distinct frame sentinel
`0xa5c3e18796b4d2f0`. ENTER4096,0's final SP is `base+4088`. Only the absent case
makes `[base,base+4096)` PROT_NONE. Thus the initial8-byte push is wholly in a
writable page and the final hypothetical8-byte check is wholly in the protected
page. Nesting0 never dereferences old RBP; its unusual value is intentional data.
The writable and alloc0 controls distinguish a broken push fixture from the
later-check behavior. Initial memory is read from the child before execution,
and frame memory is read at its **first** signal-delivery stop, before any user signal
handler could change the test stack. The Linux exception and ptrace machinery
has already run, and is part of the observation boundary. No C frame uses the substituted stack.

## Output and interpretation

The producer prints fixed version `natemu.x86-semantics.v2`, applicability,
addresses, expected stop locations, raw general/segment registers, flags, DR0/6/7,
siginfo fields, initial/final frame word and cleanup statuses. It records actual
RIP/fault address even on an invalid stop. The final `complete=6 controls_valid=1`
means only that six observations and their method controls completed. Exit0 is
not architecture acceptance; exit2 means method/control error and exit3 means
inapplicable host. Review raw output and disassembly, not merely exit status.

For the disputed debug case, record the observed logical causes without merging
or clearing them in software. For disputed ENTER, distinguish old sentinel,
saved old RBP, or another value without forcing either preferred result. The
ordinary-RAM conclusion must remain separate from Intel's explicit MMIO effects
rule. No MMIO behavior is exercised here. Independent findings review must check
all controls and host applicability before an observation influences the design;
replication, another CPU model, or a changed producer needs its own budget.

## Retention commands

Source-only preparation on macOS (choose a fresh name; never overwrite):

```sh
NATEMU_RAW_ROOT=/private/tmp/natemu-track03-raw python3 experiments/track03/x86-semantics/run.py --name x86-semantics-source-01 --source-only
```

On the applicable Linux host, default mode builds and disassembles but does not
execute the producer:

```sh
NATEMU_RAW_ROOT=/tmp/natemu-track03-raw python3 experiments/track03/x86-semantics/run.py --name x86-semantics-build-01
```

Only after independent method review and a host-specific run budget may a fresh
invocation add `--execute`; neither source capture nor a successful build grants
that budget. The wrapper retains its manifest, source inputs, command logs and
checksums outside the repository.

## Explicit AMD/KVM exploration

Schema2 preserves the six cases and their stop-on-control-failure behavior; it
only adds explicit applicability selection and purpose metadata. Earlier schema1
preparation snapshots remain historical and are not rewritten. Source capture or
build may include `--explore` to record intended applicability. Only the root's
bounded authorized remote run uses both `--explore --execute`, with a fresh name:

```sh
NATEMU_RAW_ROOT=/tmp/natemu-track03-raw python3 experiments/track03/x86-semantics/run.py --name x86-semantics-amd-explore-01 --explore --execute
```

This is one45-second invocation, at most six fresh children, no retries and no
privileges. Failed Intel-derived control expectations on AMD/KVM terminate the
invocation and remain exploratory observations; they do not trigger changed
controls or a rerun. Neither success nor failure closes claims51/55. The default
mode still rejects non-Intel and advertised virtualization. Direct producer use
also requires explicit `--explore`; unknown arguments are rejected.

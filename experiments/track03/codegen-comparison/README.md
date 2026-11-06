# Bounded codegen comparison

Current evidence: `comparison-guarded-primary-01` and independently rebuilt
`comparison-guarded-replica-01`, retained under
`/private/tmp/natemu-track03-comparison-raw`. Each passes204 admission and294
timing outputs. INV-03-01 claim20 owns selection; its findings thread owns the
finite comparison acceptance. Later sections retain preparation history.

To reproduce from the sealed primary, choose a fresh run name:

```sh
NATEMU_RAW_ROOT=/private/tmp/natemu-track03-comparison-raw \
python3 -B experiments/track03/codegen-comparison/comparison.py \
  --name comparison-guarded-replica-new \
  --raw /private/tmp/natemu-track03-comparison-raw \
  --guarded-fp --purpose decision-evidence \
  --rebuild-from /private/tmp/natemu-track03-comparison-raw/comparison-guarded-primary-01 \
  --execute
```

This requires the recorded M4/macOS/HVF signing environment and retained raw
artifacts. The driver verifies snapshot hashes and its own byte identity, builds
from that snapshot, and refuses reused output names. A new execution requires
its own bounded research allocation; the completed campaign assigns no rerun.
The historical machine result key `exploratory_body_timings` is not an evidence
verdict; schema-v2 manifest purpose and independent review establish run use.

## Historical preparation

The original preparation method and budget were recorded in INV-03-01. No
generator was selected at this preparation stage. `setup.py` prepares a shared
Linux ARM64 toolchain from the already installed pinned image, with its database
entrypoint replaced by the retained setup script. It builds no generator and
executes no guest fixture. Use an external, fresh `NATEMU_RAW_ROOT` and `--name`.

It reuses frozen retention functions without editing them, retains package files,
versions, commands, source snapshot and toolchain image, and removes only its
own named container even on failure. The container has two CPUs, 8 GiB memory,
no swap, and a 30-minute setup wall budget; package-download admission is 700 MiB.
Campaign source/network/disk accounting remains with the owning investigation;
this setup alone does not claim to enforce all later campaign budgets.

FEX preparation (`/root/comparison_sources`, September 20, 22:28:22–22:32:41 UTC;
4m19s charged to the FEX allowance) adds `fex-export.py` and `fex.sh`. Root must
supply a pinned `source/dependency-overlay.tar` and `source/dependencies.json`;
the shell performs no dependency fetch. It builds only CodeSizeValidation,
checks NASM output against all six exact byte fixtures, retains its calibration
NOP plus six full blocks and raw relocation bytes, and guards guest execution.
Raw INFO and code/tail boundaries remain available beside normalized upstream
counts. Exit 1 is admitted only with completed exports and the upstream count
or cache-version mismatch message; it is not a successful upstream count test.
Raw relocation bytes are producer-layout data, not an implemented relocation API.

Author checks: exact retained-source patch anchors, Python syntax, shell syntax,
synthetic matched/corrupt input, and complete/truncated/unexplained-nonzero output
controls passed. No FEX source was compiled or executed in this preparation;
these checks establish neither C++ build success nor generator performance.

Actual campaign outcome: all three generators built and exported seven fixtures
in `box64-export-02`, `fex-export-01` and `tcg-export-02` beneath
`/private/tmp/natemu-track03-comparison-raw`. Earlier preparation-only statements
above describe author checks at that time. Failed attempts remain alongside the
successful runs. No fixture execution or timing occurred. INV-03-01's completed
exploration section owns the artifact boundaries, independent checks, budget
closure and proposed next experiment; raw byte counts cannot rank candidates.

## Captive leaf extraction — FEX and TCG, September 20

Author `/root/comparison_sources`, bounded source/artifact derivation beginning
22:48:26 UTC and ending 22:52:04 UTC (3m38s); no raw run was changed and no code executed. These are **unchanged
byte slices**, not handwritten equivalents or accepted adapter implementations.
Source roots below are the sealed runs under
`/private/tmp/natemu-track03-comparison-raw/`; the integration needs independent
review and runtime checks. Offsets are bytes, with exclusive ends.

| FEX fixture | Retained file under `fex-export-01/outputs/` | Copy range | Length |
| --- | --- | --- | --- |
| Calibration NOP | `000.native` | `[12,12)` | 0 |
| bare_load | `001.native` | `[12,20)` | 8 |
| live_load | `002.native` | `[12,56)` | 44 |
| bare_store | `003.native` | `[12,20)` | 8 |
| live_store | `004.native` | `[12,56)` | 44 |
| bare_alias_load | `005.native` | `[12,20)` | 8 |
| live_alias_load | `006.native` | `[12,56)` | 44 |

Each FEX end points at the retained little-endian instruction word `0x0000420f`
(`UDF #0x420f`). Start12 excludes the four-byte block metadata and the `ADR x0` /
`STR x0,[x28]` bookkeeping prefix. The full body, including emitted NOPs and
live fixtures' final `CFINV`, is retained. None of these slices contains a
branch, call, PC-relative address or reference to the exported relocation table.
Their load/store addresses come entirely from initialized registers. NOP is an
empty semantic-body control; do not substitute a new NOP and call it emitted
code. Add the same explicit wrapper return/control mechanism to all slices.

**FEX bindings.** `RAX=x4`, `RCX=x7`, `RDX=x5`, `RBX=x6`, `RDI=x11`,
`XMM0=v16[127:0]`, `XMM1=v17[127:0]`; raw PF is `x26`, raw AF is `x27`, and
SF/ZF/inverted-CF/OF occupy NZCV.N/Z/C/V. The sliced bodies do not access `x28`
or any canonical-state slot. Initialize all named guest inputs; normal loads
use `[x6]`, alias loads use incoming `[x4]`, stores write incoming `x4` to `[x6]`.
Live fixtures also write `v16 XOR v17` to the 16 bytes at `[x11]`. Use valid,
canonical addresses, an eight-byte-aligned scalar region, a sixteen-byte-aligned
vector region, and ordinary writable WB memory; retain nonoverlapping regions
unless an alias is deliberately part of the declared control.

Bare loads change only `x4`; bare stores change scalar memory. Both preserve
NZCV, raw PF/AF and other inputs. Live bodies change `x7` (64-bit ADD result),
`x5` (only its low byte becomes SETZ), `v16`, `x26`, `x27`, NZCV and scratch
`x20`; load variants also change `x4`, and store variants change scalar memory.
All live bodies change the vector destination memory. `x6`, `x11`, `v17` and
other registers remain untouched. Preserve wrapper ABI callee-saved `x20`,
`x26`, `x27`; preserve additional registers if the wrapper uses them. There are
no emitted SP/LR accesses in these slices. Snapshot NZCV with `MRS` immediately
at body exit, before wrapper comparisons or loop control overwrite it.

For arbitrary incoming bare-fixture arithmetic flags, one valid encoding is
`x26 = !PF`, `x27 = AF << 4`,
`NZCV = SF<<31 | ZF<<30 | (!CF)<<29 | OF<<28`.
For live fixtures, the emitted `ADDS` produces the sum in `x26`, while `x27`
contains the original RCX XOR RDX. After the retained `CFINV`, decode
`CF = !NZCV.C`, `SF=NZCV.N`, `ZF=NZCV.Z`, `OF=NZCV.V`,
`PF = even_parity(x26 & 255)`, `AF = ((x27 ^ x26) >> 4) & 1`.
Other guest flags remain the wrapper's unchanged input values. Native FPSR/FPCR
are not modified by these integer/SIMD logical bodies. Software ordering remains
exactly emitted: scalar `LDAR; NOP` or `NOP; STLR`, and `DMB ISH` before the live
vector `STR Q16`. `CFINV` requires the admitted host FlagM capability; it must
not be replaced silently for a different host.

Bindings are supported by the sealed FEX `outputs/fex.log` disassembly,
`upstream/FEXCore/Source/Interface/Core/ArchHelpers/Arm64Emitter.{cpp,h}` SRA and
PF/AF constants, `OpcodeDispatcher/Flags.cpp` `LoadAF`/`LoadPFRaw`, and
`JIT/ALUOps.cpp` `CarryInvert`. Body bytes were read directly from the seven
retained native files and their end-marker words checked.

**TCG slices and state.** For files under `tcg-export-02/outputs/`, copy all
`bare_*.native` at `[16,32)` (16 bytes), all `live_*.native` at `[12,128)`
(116 bytes), and `nop.native` at `[16,16)` (empty). These spans exclude the
entry poll, RIP materialization, branch exits and separate shared prologue.
They retain all canonical guest-result stores, including the live `cc_op` store
at byte124. They have no external control transfers or PC-relative references.
The live span retains the one-byte `can_do_io` store at `env-12`; allocate at
least16 writable bytes before the environment pointer rather than dropping
that instruction.

Set `x19=env`. Concrete sealed-build offsets from `env` are: RAX0, RCX8, RDX16,
RBX24, RDI56 (all eight bytes); `cc_dst`272, `cc_src`280 (eight bytes), `cc_op`296
(four bytes); XMM0 at960 and XMM1 at1024 (sixteen bytes each). Allocate through
at least byte1039 after `env`, and initialize/preserve sentinel padding. Bare
loads clobber native `x20`, bare stores `x20/x21`; live spans clobber `x16`,
`x20/x21/x22`, `v0/v1`, NZCV, and additionally `x23` for live_store. Guest
outputs reside in the canonical slots and destination memory, not those native
scratch values. `x19`, SP and LR are unchanged. Save the wrapper's callee-saved
registers that overlap these scratch registers.

Live TCG stores `cc_dst=sum`, `cc_src=original RDX`, `cc_op=11` (`CC_OP_ADDQ`).
Recover original RCX as `a=cc_dst-cc_src` modulo64 bits; with `b=cc_src,r=cc_dst`,
CF is `r<b`, PF is even parity of the low byte of `r`, AF is bit4 of `a^b^r`,
ZF is `r==0`, SF is bit63 of `r`, and OF is bit63 of `~(a^b)&(a^r)`.
Incoming bare-fixture condition-code state is untouched. TCG's native NZCV at
exit is a scratch comparison result and must not be presented as guest flags.
Scalar ordering is emitted `DMB ISHLD` plus load or `DMB ISH` plus store; the
live vector write uses `DMB ISHST; STP`. Derivation uses sealed disassembly/IR,
actual instruction words and `upstream/target/i386/{cpu.h,tcg/cc_helper.c}`.

**Comparison limit.** These spans are concretely extractable for bounded
successful ordinary-memory execution. FEX retains guest values in native
registers; TCG materializes them inside the slice. A body-only timing exposes
that implementation difference but excludes FEX's canonicalization cost.
Report a common initialized-input/observable-output wrapper boundary separately,
or explicitly describe cached-state residency as the measured assumption.
Neither slice updates RIP or supplies fault/interrupt recovery. Record such
excluded work as unmeasured; do not price it at zero. Repeated iterations must
reset or model changed RCX/RDX, vectors and alias-load addresses identically for
every candidate, and correctness must cover the full live-out state above.

## Captive execution exploration

`execute.py --raw <sealed-emission-root> --name <fresh-run>` builds from a fresh
snapshot; add `--execute` for the current bounded exploratory campaign. Set
`NATEMU_RAW_ROOT` outside the repository. `execution.c` reuses the native-handoff
probe's stage1/TSO idioms. The retained generation includes exact byte slices,
provenance hashes, input images, schedule, assembly and wrapper disassembly.
`check_execution.py` owns the independent result/admission oracle.

The common wrapper uses x30 for a non-flag-setting SUB/CBNZ loop, with serialized
CNTVCT reads and a final drain. No platform function calls enter the translated
body. Initial guest inputs are reset between samples, but repeated iterations
naturally evolve state inside each sample. Alias loads follow a two-node cycle.
All captured barriers remain; enabling hardware TSO does not remove them. Native
state, vectors and memory are captured outside the timed region after HVC.
NOP has an empty semantic slice and measures this loop only. No blind subtraction
of that control turns different state conventions into equivalent runtime costs.

One fresh VM/vCPU uses256KiB RAM; code, candidate state, guest data and stack are
separate. Boot checks stage1/TSO and FlagM admission. The two ADD seeds exercise
zero/carry and signed overflow; repeated execution reaches a stable scalar state
and toggles vector data. First run144 correctness/fault cases; only an accepted
admission permits378 randomized timing samples. Native unmapped faults validate
capture at the actual scalar access and retained partial effects, not full guest
fault delivery. Clock qualification and correctness are reported separately.
Each phase has90 seconds,180 total; no retries inside the runner. A failed run is
retained and any corrected run uses a new identity within the campaign allowance.

### Hardware-TSO lowering slice audit

Independent reviewer `/root/comparison_sources` inspected the fresh sealed
`box64-hardware-01`, `fex-hardware-01` and `tcg-hardware-01` outputs under the same
raw root. The profile patches were authored by `/root/direction_review`.
This is verification of fresh emitted-byte differences and bindings for the
exploratory integration, not independent reacceptance of the earlier FEX binding
analysis or a runtime/performance finding.

| Candidate | Empty NOP | Every bare body | Every live body | Scalar fault offset from body start |
| --- | --- | --- | --- | --- |
| Box64 | `[0,0)` | `[0,4)` | `[0,56)` | bare0; live36 |
| FEX | `[12,12)` | `[12,16)` | `[12,48)` | bare0; live16 |
| TCG | `[16,16)` | `[16,28)` | `[12,120)` | bare load/alias4, store8; live load/alias44, store48 |

All 21 full-native hashes match their sealed manifests. Comparing actual
instruction words proves that Box64 and TCG body differences are solely removal
of the emitted DMB instructions. FEX differences are removal of its emitted
NOP/DMB instructions and replacement of LDAR/STLR by same-register LDR/STR.
The FEX UDF boundary words remain verified at the new ends; Box64 and TCG
printed instruction words match native bytes. Guest state/register/flag bindings,
canonical writebacks and scratch-register sets remain unchanged. The exact new
body hashes and fault offsets were delivered directly to the independent checker
author; the execution checker must match those sealed slices before admission.
These profiles rely on separately enabled/read-back hardware TSO; single-vCPU
fixture execution does not establish a new SMP ordering result.


Paired execution completed in `execution-paired-01`: add `--paired-profiles` to
build42 bodies from both sealed profile families. This mode performs288 admission
cases and756 longer samples inside the same180-second execution ceiling. Exact
profile bytes, ranges and inputs pass `check_inputs` before any VM execution.
Both output oracles passed; long-batch clock sanity passed. INV-03-01 owns the
exploratory numerical results, their limits and the closed campaign budget.
`execution-software-01` retains an invalid host-counter reading; `-02` corrects
the assembly and preserves identical non-timer outputs. No selected generator or
complete captive adapter is claimed by any of these runs.

## Guard and resident-boundary preparation — September 20

Author `/root/direction_review`, bounded eight-agent-minute subtask of the
23:14–00:14 guard/boundary campaign. This derives admission semantics from
INV-03-01 claim15 and reviews the comparison direction; it does not implement
or independently approve the eventual producer/checker.

For an ordinary indivisible access of fixed length `n=8` or `16`, after guest
address-size/segment processing, claim15 admits unsigned `E=A+n-1` only when
it does not overflow and both endpoints occupy the same x86 canonical half.
This six-instruction construction uses two reserved temporaries and preserves
native NZCV throughout:

```asm
// A is unchanged; T/U must be distinct and must not alias A.
sbfx T, A, #0, #48
eor  T, T, A
add  U, A, #(n-1)
eor  U, U, A
orr  T, T, U, lsr #47
cbnz T, rejected_access
```

The first XOR is zero exactly when A equals its sign-extended48-bit value.
The shifted endpoint XOR is zero exactly when bits63:47 of A and E agree.
For fixed positive8/16-byte spans, those conditions imply canonical E, the same
canonical half and no unsigned wrap; every rejected condition produces nonzero T.
None of these instructions changes flags. `x8/x9` are unused by the selected
bodies and can be reserved, but their clobbers must be part of the wrapper and
oracle contract. TCG may need an explicit load of the address from its trusted
state; account for that adapter work rather than charging every candidate a
fictional common state load.

| Control | 8-byte start | 16-byte start | Expected |
| --- | --- | --- | --- |
| Ordinary mapped RAM | `0x40020000` | `0x40020100` | Admit and execute |
| Last low-half full span | `0x00007ffffffffff8` | `0x00007ffffffffff0` | Admit predicate |
| Low-half span crosses into hole | `0x00007ffffffffff9` | `0x00007ffffffffff1` | Reject before operand access |
| First high-half address | `0xffff800000000000` | `0xffff800000000000` | Admit predicate |
| Last nonwrapping high span | `0xfffffffffffffff8` | `0xfffffffffffffff0` | Admit predicate |
| Unsigned endpoint wraps | `0xfffffffffffffff9` | `0xfffffffffffffff1` | Reject before operand access |
| Low noncanonical start | `0x0000800000000000` | `0x0000800000000000` | Reject |
| High noncanonical start | `0xffff7fffffffffff` | `0xffff7fffffffffff` | Reject |

Predicate-only controls need not dereference these extreme addresses. Keep
predicate acceptance separate from mapping admission: a canonical unmapped
address passes this guard and then takes the existing native mapping fault.
An independent integer predicate should check endpoint overflow and the two
halves directly, not repeat the assembly's XOR construction.

Place each guard immediately before its corresponding guest operand access;
do not preflight all memory operands before the five-instruction body. A rejected
late vector access must not suppress already completed scalar/ADD/XOR effects.
Trusted bounded state accesses are not guest operands. Rejection goes to the
probe's declared rejection/capture path without the unchecked access, not to an
invented x86 exception policy. This control establishes neither MMIO semantics
nor complete fault priority/recovery. Shadow permissions and access classification
remain distinct requirements.

For the resident-boundary sweep, claim15 permits live registers and deferred
flags to remain noncanonical, and claim18 explicitly rejects universal canonical
spills. Therefore compare a helper with an explicit input/output/clobber contract
that preserves only affected live roots, separately from forced materialization
of the measured guest state. Full spills every K bodies are a sensitivity case,
not an intrinsic FEX/Box64 cost or mandatory runtime policy. Retain the cached
path, apply identical helper semantics and frequencies at K=1/4/16/64, and charge
only actual candidate-specific preservation, guard and materialization work.
This can test whether the observed cached-body lead survives the chosen boundary;
it cannot establish all helper/branch/dispatch costs or select a workload-wide
winner. A common forced canonicalization policy must not recreate TCG preference
by construction.

## Compile-duration instrumentation — consolidated campaign

Author `/root/direction_review`, within the consolidated campaign's bounded
preparation allowance. Fresh exporter builds emit `NATEMU_COMPILE` records using
in-process `CLOCK_MONOTONIC`; clock-read failures or negative elapsed values fail
the export. These measure the actual Linux AArch64 worker/toolchain/build context,
not macOS host compiler latency. They do not change fixture execution refusal,
exact decoded-input admission or the existing six-fixture/NOP protocol.

| Candidate | Timed interval and cache policy | Included/excluded costs |
| --- | --- | --- |
| Box64 | One `CreateDBnoAlt` invocation, first compile in each fresh initialized process | Includes compiler logging; excludes prior ELF/setup work and subsequent raw export I/O |
| TCG | `tb_gen_code` entry through generated code/search diagnostics, stopping at the existing prepublication export | Includes frontend/backend diagnostics; excludes process/setup, export I/O and remaining TB publication/return work |
| FEX | One `CompileRIP` (calibration NOP), then six `CompileRIPCount` invocations in the same initialized process | The existing tool invalidates its code-buffer/thread-cache range before each compile; invalidation/input-copy time is outside the interval. Includes disassembly/parser diagnostics and raw export I/O that occur inside the compiler call |

The FEX records carry sequential indices0–6 and its output checker requires all
seven. Earlier invocations may warm allocators/code/pages even after range
invalidation. Box64/TCG first-compilation initialization differs from that policy;
diagnostic and I/O boundaries differ too. These single-shot observations therefore
must not be presented as a cold-compile ranking or substituted directly into a
matched reuse crossover. Report the measured context and its uncertainty. A
matched diagnostics-off compilation experiment would be separate consequential
work only if these unresolved costs can change the decision; this instrumentation
does not open such a campaign automatically.

Author checks applied all three patch scripts to fresh copies of the exact pinned
archive members and checked Python syntax/source anchors. No generator execution,
container, source download or guest fixture execution was performed by this subtask.

### Resident-boundary contracts for the existing live fixture

`/root/comparison_sources`, September 20, bounded preparation 23:15:00–23:17:57 UTC (2m57s).
This derives a **synthetic adapter experiment**, not upstream-generated boundary
code. INV-03-01 claim15 permits native/deferred roots with declared helper inputs,
outputs and clobbers; neither it nor claim18 requires universal canonical spills.
Reuse the preceding accepted scoped bindings, without a new state-field audit.
The tested subset is the five named GPRs, arithmetic flags, XMM0 and XMM1; it
cannot price a full x86 state snapshot or arbitrary guest helper.

Two distinct boundaries can answer different cost questions:

1. **Read-only resident view.** A helper consumes the five guest GPR values, two
   vectors and semantically decoded arithmetic flags while preserving declared
   native roots. Box64 and TCG already hold vectors/deferred flags in resident
   memory; TCG also holds the five GPRs there. FEX exposes native vectors,
   raw PF/AF and a captured NZCV. A tagged deferred-flags view is valid, but its
   decode must occur inside the measured helper interval before a common digest
   consumes flags. Hashing different generators' raw flag words is not equivalent
   semantic work. No reload is necessary if the actual helper preserves the roots.
   Price any gather/spill needed by the chosen call ABI, rather than assuming a
   zero-cost view or forcing a full record copy. A preserve-root/custom leaf ABI
   can be useful; it is not interchangeable with arbitrary C helper code.
2. **Canonical snapshot and clobber/reload.** Define an 80-byte aligned record:
   RAX0, RCX8, RDX16, RBX24, RDI32; arithmetic flags40 (`0x8d5` mask); XMM0 bytes
   48–63 and XMM1 bytes64–79. Materialize it, call a helper that consumes it without
   changing guest state, and restore the declared clobbered live roots. The
   helper's non-guest result is a verified digest of these same ten 64-bit words.
   Charge flag decoding and reconstruction. This explicit dense-copy record is
   a snapshot-pressure scenario: copying TCG's already materialized state to a
   second layout and back is an artificial cost of this chosen consumer ABI,
   not a mandatory captive runtime operation. A no-change helper alone does not
   justify reloading; its verified clobber contract must require the reload.

| Candidate | Publish the five GPRs / vectors | Canonical reload, if declared roots were clobbered |
| --- | --- | --- |
| FEX | `x4,x7,x5,x6,x11`; `q16,q17` | Reload those registers. Encode arithmetic flags as `x26=!PF`, `x27=AF<<4`, and NZCV=`SF<<31 | ZF<<30 | (!CF)<<29 | OF<<28`. This is an equivalent raw recipe, not the original ADD-result recipe. |
| Box64 | `x10,x11,x12,x13,x17`; memory at `x0+0x90` / `x0+0xa0` | Reload five native GPRs and restore the resident vector slots. Combine arithmetic flags with preserved non-arithmetic bits in `x26`, and set deferred-kind word `[x0+0x44c]=0` (`d_none`). Native q0/q1 are scratch reloaded by the next existing body, so restoring them is unnecessary. |
| TCG | Memory at env+`0,8,16,24,56`; env+`960,1024` | Restore those state slots if the chosen dense-copy/clobber test changed them. Canonical flags go to `cc_src` at env+280 and `cc_op` at env+296 becomes0 (`CC_OP_EFLAGS`); old `cc_dst/cc_src2` are then irrelevant. Native scratch GPRs/q0/q1 do not need reloading for the next body. |

The record contains arithmetic flags only; preserve other guest flags separately.
For read-only boundaries, retaining the existing deferred representation avoids
unnecessary normalization/re-encoding. FEX's live decode uses captured NZCV with
inverted carry plus raw x26/x27. Box64 uses `(d_add64,C,D,S)` and falls back to
old x26 for `d_none`. TCG uses `(CC_OP_ADDQ,S,D)` or raw `cc_src` for
`CC_OP_EFLAGS`. All ADD formulas are already recorded above; do not treat
Box64's old x26 or TCG's native NZCV as current arithmetic flags.

Under an ordinary AAPCS64 call, the FEX five guest GPRs, q16/q17 and NZCV are
caller-clobbered; raw PF/AF in x26/x27 are callee-preserved. Box64's five guest
GPRs and x0 state base are caller-clobbered, but its resident vectors/deferred
flags remain available. TCG's env in x19 is callee-preserved and its guest values
are already resident. A custom preserve-root helper changes these burdens, so
record the actual ABI and inspect its clobbers. Preserve the probe's x30 loop
counter around BL/RET separately from guest-state costs. Capture NZCV before
any adapter arithmetic changes it, and restore FEX NZCV last when needed.

Report boundary-inclusive time alongside the original body-only baseline and a
helper-only control; do not call the measured dense-copy overhead universally
unavoidable or extrapolate one boundary frequency to all workloads. Correctness
must verify canonical record fields, unchanged guest state, the helper digest,
and the next body's continuation—not merely a digest that could conceal swapped
or omitted fields. These finite measurements can expose whether realistic helper
frequency reverses the body-only ordering, without pricing recovery, general
state images or x87 accuracy work that this fixture does not exercise.


### Standalone numerical cost/accuracy comparison — September20

Author `/root/comparison_sources`, within the consolidated final exploration's
25-agent-minute numerical allowance; preparation began23:44UTC, producing run
completed23:48UTC. No download. `numerical.cpp` and `numerical.py` are disposable
kernel/retention code. This does not implement an x87 frontend or a captive helper
ABI. Root independently reviews the authored method and findings.

Retained run: `/private/tmp/natemu-track03-comparison-raw/numerical-02`.
`numerical-01` preserves a sandbox Docker socket denial before producer execution.
The successful run snapshots and checksums the actual native ARM64 libraries and
headers against sealed `fex-hardware-01`; all compilation consumes that snapshot.
It records the immutable image, compiler, libc/runtime library hashes, source
provenance, binary, disassembly,26 accuracy rows and56 checked timing rows. Docker
networking is disabled; the temporary2CPU/2GiB container is removed. No guest VM
code executes. Compiler is Clang19.1.7, libc Debian glibc2.41, Linux ARM64 in
OrbStack on the same Apple host. Native FPCR is0; software rounding is nearest-even
with80-bit precision. Exception flags are recorded but not qualified.

Actual producing paths at retained FEX pin
`48d71752e2a363d5535181f803b0b0e9666c1825`:

- `FEXCore/Source/Common/SoftFloat.h` FADD/FSUB call the retained
  `External/SoftFloat-3e` extF80_add/sub kernels. The probe calls those exact
  library implementations with their preserve_all declarations. Native binary64
  `(x+y)-x` is an explicitly labelled control, whose retained disassembly is
  FADD/FSUB; it is not an exported x87 lowering.
- The same header's FSIN finite path calls extF80_to_f128, the retained Cephes128
  sinl and f128_to_extF80, including the infinity predicate. Probe code reconstructs
  that narrow call sequence and links the actual upstream implementations.
  `F80Fallbacks.h` OP_F64SIN calls libc sin; the probe's binary64 sine invokes that
  same installed libc implementation. This excludes FEX profiling and
  ScopedSoftFloatState/frame wrappers and does not claim Box64 or QEMU helpers.

Cancellation inputs are `(2^e+1)-2^e`, e52..63, all operands exactly representable
in both formats; the exact answer is1. Binary64 returns1 at52 and0 at53..63;
ext80 returns1 throughout. The fourteen sine inputs include signed dyadics in
[-1,8], zero,2^-60 and the extended-only `1+2^-60`. The last case explicitly
includes binary64 input rounding to1. A separate Decimal120-digit Taylor
calculation uses the exact dyadic input, terminates below1e-105 and has no
argument-reduction constant for this small domain. Observed errors on ordinary
nonzero inputs are about1e-18..5e-17 for binary64 and1e-21..3e-20 for ext80;
zero is exact, and both formats round sin(2^-60) to its input. These constructed
cases are not an application accuracy distribution or Intel FSIN qualification.

| Measured kernel/control | Long-batch median ns per operation/pair | Batch range ns |
| --- | ---: | ---: |
| Native binary64 cancellation control |0.770|0.763–3.296|
| Actual ext80 add/sub kernel pair |14.491|14.452–29.086|
| libc binary64 sine |3.571|3.416–3.686|
| Actual ext80/Cephes128 sine kernel sequence |228.172|227.829–229.530|

Seven alternating-order batches use100k/400k cancellation pairs and10k/40k sine
calls. Actual calls survive compilation, and every timed checksum agrees with
its admitted result table. Median elapsed-time scaling for fourfold iterations
is3.92–4.03 across the four paths. Arithmetic outliers show scheduler sensitivity;
there is no independent timing replication or calibrated captive clock claim.
Loop/index/call overhead is included; input conversion is outside timing, while
sine's ext80/f128 conversions are inside. The measured ratios are approximately
18.8x for the extended cancellation pair and63.9x for extended sine on this input
mix. These are standalone kernel comparisons, not generator or emulator ratios.
They establish a concrete accuracy/cost tradeoff supporting the owner's priority
that expensive x87 accuracy is secondary. They cannot establish application
compatibility, an acceptable numerical tolerance, or a mandatory full80 policy.

### Consolidated captive bindings

`comparison.py` uses the retained Box64/TCG bindings and the actual
`fex-consolidated-02` objects. FEX offsets are from each raw object, including its
header: branch_zero `[12,32)` with B.EQ at28 and load at16; branch_carry `[12,48)`
with B.LO at44, load at24 and CFINV before the branch. Branch target displacements
are relocated to explicit taken/fallthrough veneers; emitted conditions remain.
The veneers record actual taken count and candidate-relative guest RIP. They
return to the finite probe loop, not a stock guest program or upstream dispatcher.
Box/TCG ranges, recipes and exclusions are in `consolidated-bindings.md`.

FEX scalar add/sub and multiply/divide use `[12,20)`; sqrt `[12,16)`.
Their retained native q16/q17 inputs require AFP merging. The pinned
`FEXCore/Source/Interface/Core/ArchHelpers/Arm64Emitter.cpp` fill path enables
FPCR.NEP(bit2) and AH(bit1); the probe sets/readbacks FPCR6 for all candidates,
checks ID_AA64MMFR1_EL1.AFP and retains FPSR/FPCR outputs. It uses nearest rounding,
normal finite exact-result cases with nonzero distinct upper-lane sentinels;
this is not an exceptional SSE-state or all-rounding conformance result.
FEX integer DIV retains `[12,28)` and `[68,84)`, retargeting actual CBZ at24
into its actual UDIV/MSUB fast path. The actual XOR makes the high dividend zero;
nonzero divisors are an input admission condition. An impossible alternate edge
traps rather than calling an unbound runtime helper. Box uses the independently
recorded equivalent restricted extraction; no DIV-flag equivalence is claimed.

The actual QEMU helpers are imported from `tcg-helpers-03`: copied pinned upstream
fpu/int/SoftFloat units with their original build configuration, function sections
and static garbage-collection linking. The10,556-byte blob is linked at
0x40008000, has no unresolved symbols or writable LOAD data, and fits the existing
64KiB native-code ceiling. Literal helper targets in actual TCG bodies are relocated
to these symbols; BLR instructions and upstream arithmetic kernels remain intact.
The original helpers' exception/assertion escapes reach distinct probe HVC stops,
not guest exception delivery. CPU-state initialization comes from the actual
`cpu_init_fp_statuses` and `cpu_set_mxcsr(0x1f80)`; the15312-byte template supplies
SSE status at936 and MXCSR at944. The enlarged32KiB snapshot still fits the
original private STATE-to-DATA interval. Native helper stack storage is below
SP, distinct from the positive-offset diagnostic counters.

Compilation self-checks found that Mach-O cannot express the proposed bitwise
relocation through `.long`; generation now spells the decoded original B.cond or
CBZ mnemonic with its unchanged condition/register and a new target. No source
emitter instruction is replaced with an oracle-computed condition. Full retained
build disassembly and independent guest-output checks must precede timing claims.
Per-body counters and RIP recording remain timed common diagnostic overhead;
NOP is a control, not permission to subtract that cost blindly. Guest program
branch frequencies, external admission contention, actual cold fault completion,
full SIMD/FP semantics and end-to-end workloads remain outside this finite probe.


## Consolidated branch/FP/helper execution — closed exploration

`comparison.py` snapshots the six expanded fixtures plus NOP, retained candidate
outputs, guarded branch veneers and actual linked QEMU helpers. The independent
`check_comparison.py` checks exact inputs, retained relocation declarations,
architectural state, preserved native roots, actual branch counts/RIP and FP controls.
`comparison-execution-01` passed168 admission and294 longer output checks; only
longer samples passed clock sanity. INV-03-01 owns the recorded medians and limits.

A consequential source limitation prevents a general FP ranking: pinned QEMU
`softfloat.c::can_use_fpu` requires sticky inexact before its native fast path.
These exact clear-status inputs stay on the software path throughout. FEX
`Vector.cpp::GetMXCSR/RestoreMXCSRState` masks away sticky exception flags, so
its exact native outputs likewise do not establish the required SSE contract.
See `consolidated-bindings.md` for actual REP/recovery and status adaptation work.
The campaign closes with selection insufficiency; no additional run is assigned.

# INV-01-34 — findings review

## Screening

1. **Shared problem:** QEMU TCG and ordinary x86 VMs also start secondary
   CPUs, but INIT/SIPI provides their usual route. Excluding real-mode
   execution is the relevant constraint of this shim design.
2. **Existing documented answer:** ACPI specifies the mailbox handoff and
   Linux implements it. Those contracts require source analysis, not an
   experiment that merely repeats the documented protocol. Faithful realization
   together with the earlier shim mechanisms remains experimental work.
3. **Applicable design:** the host translator plus EL1 shim using ACPI MADT
   mailbox firmware emulation and the inspected Linux startup path. This review
   makes no claim about other emulator families or every distribution kernel.

## Subject, independence, and verdict

- Reviewer: agent `/root/retained_runner`, separate context from source author
  `/root/q34_sources`. The reviewer authored the general retained-run runner,
  but did not author this investigation or its source findings.
- Date: 2026-09-06.
- Material: the complete `INV-01-34` source-only submission, workflow, charter,
  introduction patch, final v5.18/v5.19 ACPI code, v5.19 APIC callback/caller/
  trampoline/flag definitions/boot protocol, v6.8 trampoline, and v6.12
  Kconfig/mailbox lifecycle code. All upstream files were fetched independently
  from the cited release URLs; the normative ACPI section was opened directly.
- **Verdict: accept the scoped source claims `CLM-01-34-01`–`07`.** This accepts
  the upstream feature boundary, protocol and inspected control flow, and the
  explicitly conditional inference. It does **not** resolve `Q-01-34`, establish
  stock-target compatibility, authorize execution, or establish host feasibility.

## Independent checks and challenges

**Release boundary (`01`).** The
[introduction patch](https://github.com/torvalds/linux/commit/f39642d0dbacded8b4a816a9197a73efb74e5702.patch)
adds the MADT parser, mailbox callback, and installation in the APIC drivers.
The implementation is absent from
[v5.18 ACPI boot code](https://raw.githubusercontent.com/torvalds/linux/v5.18/arch/x86/kernel/acpi/boot.c)
and present in
[v5.19](https://raw.githubusercontent.com/torvalds/linux/v5.19/arch/x86/kernel/acpi/boot.c).
Linux 5.19 is supported as the first upstream release of this implementation;
the TDX motivation does not create an additional TDX runtime gate.

**Mailbox contract (`02`).** Independently checked
[ACPI 6.5 §5.2.12.19 and Tables 5.43–5.44](https://uefi.org/specs/ACPI/6.5/05_ACPI_Software_Programming_Model.html#multiprocessor-wakeup-structure):
entry type/length, alignment and memory type, field offsets, matching-ID rule,
cache-before-acknowledgment ordering, serialized reuse, one wake per AP, and
Intel initial execution state agree with the submission. An acknowledgment
demonstrates receipt, not successful kernel initialization; the proposed later
method correctly requires a subsequent ready marker.

**Build and call path (`03`).** The
[v5.19 Makefile](https://raw.githubusercontent.com/torvalds/linux/v5.19/arch/x86/kernel/acpi/Makefile)
and nested guards in `boot.c` support the stated configuration dependencies.
The callback release-publishes the command and has an unbounded acknowledgment
loop. The
[APIC update function](https://raw.githubusercontent.com/torvalds/linux/v5.19/arch/x86/kernel/apic/apic.c)
installs the 64-bit callback across the registered drivers. The
[v6.12 Kconfig entry](https://raw.githubusercontent.com/torvalds/linux/v6.12/arch/x86/Kconfig)
is default-yes with the four named dependencies. An absent, malformed, or
unreachable MADT path does not establish the route; no vendor configuration
is inferred from these upstream defaults.

**Mode transition and implication (`04`–`05`).** The
[v5.19 caller](https://raw.githubusercontent.com/torvalds/linux/v5.19/arch/x86/kernel/smpboot.c)
selects `trampoline_start64` and prefers the 64-bit wake callback.
The actual
[v5.19 trampoline](https://raw.githubusercontent.com/torvalds/linux/v5.19/arch/x86/realmode/rm/trampoline_64.S)
loads the GDT/IDT and far-jumps via `tr_compat` to `.code32`
`pa_trampoline_compat`, disables paging, and continues through `startup_32`
before re-entering 64-bit code. The
[flag definition](https://raw.githubusercontent.com/torvalds/linux/v5.19/arch/x86/include/uapi/asm/processor-flags.h)
preserves `CR0.PE` when `CR0.PG` is removed. Thus this is a protected-mode
transition, not execution of the separate `.code16` entry. The same essential
path remains in the
[v6.8 trampoline](https://raw.githubusercontent.com/torvalds/linux/v6.8/arch/x86/realmode/rm/trampoline_64.S).
The inference that narrow 32-bit handling is necessary for these inspected
paths is sound. A test that substitutes a direct `start_secondary` call would
bypass the very behavior requiring validation.

**Lifecycle and direct boot (`06`–`07`).** The
[v6.12 mailbox code](https://raw.githubusercontent.com/torvalds/linux/v6.12/arch/x86/kernel/acpi/madt_wakeup.c)
requires the version-1 reset route for offlining; otherwise it disables
offlining and zeros the published mailbox address while retaining the current
kernel's cached physical address. A later kernel without that address receives
`-EOPNOTSUPP`. Cold boot therefore does not imply CPU recycling or kexec.
The separate
[v5.19 64-bit boot protocol](https://raw.githubusercontent.com/torvalds/linux/v5.19/Documentation/x86/boot.rst)
requires boot parameters, entry offset and architectural state independently
of AP wakeup. The submission correctly keeps that loader obligation separate.

## Substantive blockers and remaining work

No substantive blocker remains against the accepted **source-only** claims.
The following remain requirements of the open investigation, not waived defects
in this review:

- Name stock target packages and inspect their actual configuration and startup
  path. A prospective Ubuntu/Debian choice is not package evidence.
- Submit bounded method/code for integrated SMP realization with every earlier
  mechanism enabled, real mailbox ABI, unmatched-ID/no-response controls, and
  execution of the required modeled startup transitions.
- Retain reviewed experimental runs and independently rebuild/repeat any
  load-bearing observations before claiming host feasibility. Source re-reading
  is not runtime replication; this submission has no Observed claims.

## Editorial edits, limits, and returns

No direct edits were made to the investigation and no load-bearing replacement
was authored by the reviewer. Public source downloads are temporary navigation
copies, not experimental evidence. No archived repository history was accessed.
Inspection covers the named release files, not every Linux release or any
distribution binary. No macOS VM execution, timing, M4/M2 applicability, hotplug,
or stock-kernel success is accepted here. The bounded source review ends with
this verdict; subsequent experimental preparation receives its own pre-execution
review. No return was required.

## Supplement review — stock configurations and startup paths

Same independent reviewer, 2026-09-06; subject: proposed claims 08–10. The
screening and design applicability above remain unchanged. **Verdict: accept
the scoped source/configuration findings and method-candidate inference.**
Q34 still requires its reviewed integrated realization and independent runtime
replication. Ubuntu/Debian are working targets selected for this task, not an
inferred historical owner decision or proof of every distribution configuration.

The reviewer independently opened the
[Ubuntu amd64 package row](https://packages.ubuntu.com/noble-updates/linux-image-generic)
and [Debian package dependency](https://packages.debian.org/trixie/linux-image-amd64),
retrieved their published download checksums and the
[Ubuntu descriptor](https://archive.ubuntu.com/ubuntu/pool/main/l/linux/linux_6.8.0-139.139.dsc)
and [Debian descriptor](https://security.debian.org/debian-security/pool/updates/main/l/linux/linux_6.12.107-1.dsc),
and independently hashed the author's already-downloaded archives/packages.
All six artifact digests match those publisher declarations. This checks
public-source integrity; OpenPGP signatures were not verified. Archive bodies
were not redundantly downloaded, but configurations and relevant source files
were independently extracted from the verified bodies rather than trusting the
author's extracted copies.

**Claim 08:** both header-package `.config` files enable X86_64, SMP, ACPI,
X86_LOCAL_APIC, X86_5LEVEL and AMD_MEM_ENCRYPT. Debian additionally enables
ACPI_MADT_WAKEUP; Ubuntu's absent symbol is consistent with its older callback
location, not evidence that the path is disabled. Header build configurations
and package availability are accepted; successful boot or ACPI discovery is not.
The Ubuntu and Debian header digests independently match the respective
[Ubuntu](https://packages.ubuntu.com/noble-updates/amd64/linux-headers-6.8.0-139-generic/download)
and [Debian](https://packages.debian.org/trixie/amd64/linux-headers-6.12.107%2Bdeb13-amd64/download)
download pages.

**Claim 09:** Ubuntu's original trampoline is byte-identical to independently
retrieved v6.8. Review of the complete diff's file headers found no trampoline,
head_64.S or realmode/init.c change; the actual ACPI boot/APIC/smpboot hunks
preserve the claimed mailbox route and include the processor-enumeration
conditions acknowledged in the submission. Debian's patch files contain no
references to the specified startup paths. Its original trampoline matches
independently retrieved
[stable v6.12.107](https://raw.githubusercontent.com/gregkh/linux/v6.12.107/arch/x86/realmode/rm/trampoline_64.S).
The reviewer traced its LA57 comparison: mismatch takes the 32-bit path;
match takes the lock, prepares segments/stack, loads CR3 and executes a 64-bit
far return. This is a change from the originally inspected v6.8 behavior,
not a contradiction of the original version-scoped claim.

**Claim 10:** the finite transition inventory is a defensible candidate for
handwritten modeled operations; it is not accepted executable code or proof
that those operations have been correctly composed. The reviewer read both
actual `head_64.S` continuations and confirmed normal lock release follows
the per-CPU stack switch. One wording refinement was returned to the author:
qualify this as the **successful** continuation, because the APIC-ID-not-found
failure branch releases the lock and halts earlier. That failure path does not
make acknowledgment-time release safe on the successful path. Implementation
review must inspect the concrete modeled operations, including the Debian
fast-path far return and the continuation, rather than treating the mnemonic
inventory as a complete executable specification.

No substantive blocker remains for accepting these source-only claims. No
VM/stock kernel was executed by the reviewer, and no new runtime finding is
accepted. Exact package revisions bound applicability; updates require checking
affected configuration and sources. No direct load-bearing edit was authored
by the reviewer.

## Native primary review in progress — September 7

Reviewer `/root/q34_review` is independent of the producer, oracle, primary
executor and independent replication author. This reviewer carried the exact
pre-execution review through its returns and authored no load-bearing code.
The screening and design scope above remain applicable. **No observed Q34
resolution is accepted yet:** the two-CPU primary passes, while the four-CPU
primary exhausted the declared whole-program watchdog and requires a reviewed
bounded remedy and independent replication.

The reviewer independently rehashed all 97 entries of `q34-startup-01`
(inventory `eda66eced0d0a8fd97faeb12be1365f34bf1ba166e9d1fb08275f47d01778d04`)
and all 12 entries of its `q34-startup-01-check-01` report
(`5228029fc7f54dacca9983f94e5aafbdaae9a9c0548843d45f6a9831d09f751b`).
The exact accepted oracle finished September 7 at 21:33:31 UTC with PASS,
2,566,173 records, 193,760 cumulative native operations, 128 Q34 cases and no
unsupported replay. Nested same-VM Q30–Q33 checkpoint reports all pass.
The checker used 4,599,873,536 bytes peak sampled RSS within its 12 GiB guard.
These are correctness observations; elapsed time remains diagnostic.

Independent raw-tail counting corroborates 96 successful READY/context/native
continuation chains, 96 guest page-fault deliveries, 96 generated effects and 48
external cold-context diagnostics. The 128 global cases include six positive
variants and the diagnostic variants; they are not 128 successful stock boots.
Host logs retain stopped RAM, final foundation/TSO and Q34 host checks. The
host remains the named M4/macOS 26.6.2/SDK 26.4 with uncontrolled placement.

### Four-CPU primary watchdog return

`q34-startup-02` returned failure after the fixed 10-second host watchdog;
stopped RAM and partial logs are retained. All earlier producer checkpoint
checks passed, but no full oracle was run and no four-CPU observation is
accepted. The final trace ends with all three APs completing case 101 (variant 4,
repetition 12), followed by BEGIN102 (variant 5, repetition 12). Failure fields
remain zero. Independent symbol/disassembly inspection locates CPU0's stopped
PC `0x4001ba04` at the byte-zeroing loop for the next case's128 KiB startup
memory; peers are in ordinary Q30 barrier/park code. This supports exhaustion
of the whole-program observation window, rather than a startup-protocol stall.
It does not prove completion under a longer window.

The consequence is missing four-CPU coverage, not an architectural kill or a
waived PASS. The smallest proposed remedy is a fixed 20-second host watchdog
within the unchanged 30-second outer child and original campaign deadline,
with guest semantics, oracle and resource bounds unchanged. Exact source/build
review must precede use of the original remaining conditional VM slot; another
failure stops that campaign. Independent replication must use the accepted
remedy and retain the original failed run. This return remains open until
exact remedy acceptance and successful full four-CPU checking.

### Exact watchdog return closure and primary coverage

Producer 05 was independently accepted before the original third/final primary
VM slot. `q34-startup-03` completed all 128 cases at four CPUs, with all host
checks and stopped RAM, using 10.970 seconds for the host command under the
new fixed 20-second watchdog. All 97 retained entries independently rehash to
`d9fd5b134f99cfb64e4a63d14de707f0747935a25325ac246b17670d65fe9c01`.
The read-only completion control that rejects failed 02 accepts 03.

The full exact oracle finished at 21:50:17 UTC with PASS, 5,488,982 records,
404,450 cumulative operations, 128 Q34 cases and no unsupported replay.
The reviewer rehashed all 12 checker entries (inventory
`7769f7879b714dc84211a799c4b4fc90515990e64478cc5df8a47f9ae84f2306`),
checked exact oracle 01 source identities, terminal supervision and empty
stderr, and inspected each nested Q30–Q33 checkpoint result. Earlier cases
remain exercised in this same VM, including all 2368 applicable four-CPU Q32
CPU cases and 1792 Q33 CPU cases. Independent raw-tail counting corroborates
288 READY/native-continuation chains, 288 guest page faults, 288 generated
effects and 48 actual external Q34 diagnostic-stop records. Peak sampled
checker RSS was 6,625,984,512 bytes within the 12 GiB guard.

**Close the watchdog coverage return for the primary campaign.** The failed
02 remains a failed, incomplete observation; success 03 does not rewrite it.
The retained evidence supports an insufficient original whole-program window
and shows completion with the independently reviewed bounded remedy. No
reliability rate or timing promise follows. The campaign inventory
`ffe75aebe6056dcd3c06a7caf90fdb7e7d2164dedac0ca2b6313a77ce7f85606`
was independently rehashed: three VMs, two full checks, successful 01/03 and
failed 02 retained, ending within the original 150-minute deadline. The primary
source/version difference is explicitly limited to the host watchdog.

Primary coverage and its remedy now have no substantive review blocker.
**Observed Q34 resolution remains pending independent native replication and
review of the submitted scoped claim.** Root handed runtime ownership to the
separately reviewed replica 02 campaign; no final integrated activation is
inferred from this primary result.

## Independent native findings verdict — September 7, 22:04 UTC

**Accept scoped Observed claim `CLM-01-34-11` and the M4 two/four-vCPU
resolution of `Q-01-34`.** Reviewer `/root/q34_review` remains separate from
producer, oracle, primary executor and independent replicator. All substantive
pre-execution returns and the primary watchdog coverage return are closed.
This verdict accepts the finite mechanism realization and its stated inference;
it does not enlarge the claim to whole-stock boot or universal CPU behavior.

Independent replicator `/root/findings_review` rebuilt accepted producer 05
from its own retained snapshot, using independently reviewed preparation 02.
Both native VM/check pairs completed within the unchanged 100-minute campaign:
21:51:46–22:03:09 UTC, two VMs/two checks, no retries. The reviewer independently
rehashed both 97-entry raw runs, both 12-entry checks and the 10-entry sealed
campaign, inspected native host context/terminal logs, exact source identities,
full nested oracle results and supervision, and counted actual startup tails.

| Independent replica | Operations / records | Q34 cases / AP continuations | Peak sampled checker RSS bytes |
| --- | ---: | ---: | ---: |
| `q34-startup-replica-01` (2 CPUs) | 193800 / 2566242 | 128 / 96 | 4565434368 |
| `q34-startup-replica-02` (4 CPUs) | 404548 / 5490852 | 128 / 288 | 6594887680 |

Raw inventories are `96553255bb3fba5a466af30bbe36d9976cf9ef13534be006629babc9746aa955`
and `c781453020e5726e97ec69de9797eb872ec63c2b3fca57d676b9646e8b59b777`.
Checker inventories are `5e1b3f0f02b412590dd81341a44597ea2e18027bd6c5ba5d071ef0504d31c98b`
and `6129cf6806fa003c4fceac1557bb5d644db40b9dee8ef544df051e13f87c07e6`;
campaign inventory is `6538fb036d847c85bf40e962df95e8a959b0f1adcbf6232710fd3f9e03cba79c`.
Both checks use exact oracle 01, return PASS with empty stderr and no unsupported
replay, and preserve accepted Q30–Q33 same-VM checkpoint semantics. The independent
source/build comparison and distinct producing manifests establish replication
by rebuild and native rerun, not mere source rereading or rechecking primary RAM.

Both replica tails have 48 actual host diagnostic stops and 96/288 actual READY,
guest-page-fault, generated-effect and continuation chains, corroborating the
primary coverage. The source-backed startup-mode decisions, full backing replay,
explicit CR3/GPR/RIP/flags/selector/private-stack import and actual subsequent
native access/PF/IRET/ADD/timer behavior defeat disconnected continuation or
fabricated-ready explanations. The four-CPU case includes real failed 32-bit
BTS and earlier publisher paging/load/timer activity while an AP holds the
startup lock. The retained semantic controls challenge the corresponding
source, import, native event and diagnostic obligations; synthetic controls
are not represented as additional native samples.

The claim correctly distinguishes 128 global cases from 96/288 successful AP
chains and 48 overlapping diagnostic cases; repeated-boot refusal follows a
successful cold startup. Linux 5.19 remains the independently source-supported
mailbox-feature floor, not an unconditional distribution-compatibility promise.
The actual Ubuntu and Debian revisions, successful `verify_cpu` callee contract,
finite transition paths and explicit continuation bound the inference. Ubuntu
still requires small 32-bit protected-mode handling without clearing CR0.PE;
Debian's matched-LA57 path can remain 64-bit. No general CPUID/MSR implementation,
stock-kernel boot, production translator, lifecycle completeness, qualified
performance, reliability rate, architecture selection or unavailable-M2
coverage follows. Failed primary 02 remains retained negative/incomplete evidence.

### Completion breadth and final-run readiness

The same independent reviewer checked correctness/methodology breadth against
the charter, current five investigations' accepted scope, source/method returns
and backlog. No sixth Track 01 question or unowned implication emerged. Native
contexts and all earlier mechanisms are composed in the growing program; finite
successes do not establish broad correctness or timing. Existing owners retain
CPU/direct-boot/lifecycle contracts, startup32 generator adaptation, SMP stress
and containment, architecture/cost decisions, qualified measurements and M2
coverage. `/root/q34_oracle` separately reviewed product/architecture scope and
reported the same carryforward disposition; because that agent authored the
oracle, its breadth review supplies no independent correctness acceptance.

**The prerequisite for the separately prepared final integrated run is now
satisfied.** The reviewer inspected current final JSON pins to producer 05,
oracle 01 and supervisor 01, the unchanged 50-minute/1-VM/1-check/14 GiB/no-retry
budget, and the activation helper's exact-input verification and single-start
clock. Root may activate after fresh admission and serial handback checks.
Track closure still requires that final four-CPU full result, independent final
acceptance, current-record refresh and checksum-verified retirement of the
working scratch program while preserving retained evidence. No track closure
is inferred from this Q34 findings verdict alone.

## Final integrated result and combined breadth acceptance — September 7

**Accept the separately run final four-vCPU integrated result and completion
breadth.** The same independent reviewer inspected `q34-startup-final-01`,
its full exact oracle and sealed campaign after terminal completion at
22:13:56 UTC. All 97 raw entries independently rehash to
`093bfff8327a06b29b2f7b354141763314a5768c331125233ab37635df7b20f2`;
all 12 checker entries rehash to
`8d4788505b01ab810e4c20407d85fb707a5a2d4ddbc09e91cdf6222a39168d03`;
the 3-entry campaign rehashes to
`ec8b16dbb57612f17d8cb8f5ed4c3169be82b8fc759fcd9be79379bbc7e99bc9`.
Exact producer 05/oracle 01/supervisor 01 identities match the accepted final
preparation. The campaign used one VM and one check, with no retries, within
22:05:17–22:55:17 UTC and the fresh storage/resource guards.

The final oracle returns PASS with 404,404 operations, 5,489,617 records and
no unsupported replay. It checks the complete same-VM sequence:864 Q30 finite
cases;544 Q31 cases;592 Q32 global cases/all 2368 applicable CPU cases;
448 Q33 cases/1792 CPU cases; and 128 Q34 cases. The nested reports each pass,
including unchanged checkpoint bytes and their composed continuation, rather
than only the terminal case counter. Final raw counting corroborates 288 AP
READY/guest-PF/generated-effect/continuation chains and 48 actual external
startup diagnostic stops. Every CPU passes terminal foundation checks with
TSO2 at entry and exit; native mappings, exceptions, GIC/timer and the shared
host-device route remain active. The checker finished with empty stderr and
6,604,652,544-byte peak sampled RSS under 12 GiB. Time and memory values remain
resource diagnostics, not performance qualification.

Current Q30–Q34 owner conclusions retain their independent accepted scoped
resolutions, including Q32's closed after-WFE return and Q33's accepted
register-transfer path. Final integration introduces no contradiction or new
substantive return. Product/architecture and independent correctness/methodology
breadth dispositions above remain sound; the explicit backlog carryforward
paragraph names all surfaced downstream work without creating a sixth Track 01
question or resolving those downstream questions. No architecture or production
implementation authorization follows from this track's mechanism viability.

The reviewer also independently rehashed the 37-entry
`track01-scratch-retirement-01` inventory
`e0451acf8128000784cfcc474e9e9ec6ceab2c337a883ad1d4c1a7fbb8c96247`
and compared all 36 current scratch files against their retained copies, with
no symlinks or unlisted files. Its reproduction note correctly preserves the
difference between final working copies and historical producing inputs.
Root may now remove only the two verified working scratch directories and
refresh the charter/owner navigation to record closure; raw evidence remains.
The scientific exit criteria are satisfied. Actual retirement and final record
consistency will receive a short closing audit before declaring the track closed.

### Closing audit — September 7

**Accept Track 01 closure within its recorded M4 and finite-fixture scope.**
After root's retirement/update step, the independent reviewer confirmed both
working scratch directories are absent, rehashed the complete retained retirement
inventory again, and checked the refreshed charter, all five current owner
next actions, final-result record and backlog carryforward. No live Markdown
link still targets the removed scratch directories; dated textual paths remain
historical provenance. Actual producing snapshots and the final source/oracle/
supervisor/run/check/campaign evidence remain retained under the raw root.

The charter consistently records five accepted scoped resolutions, independent
replication, the separately accepted final integrated run, all four breadth
perspectives, downstream ownership and verified scratch retirement. Current
owners authorize no further Track 01 runs and preserve contradiction reopening.
No substantive blocker, incomplete exit criterion or new unowned implication
remains. This closes the research track, without a stock-boot, performance,
platform-wide or production-architecture conclusion.

## Owner-requested repository preservation — packaging review

The owner subsequently requested keeping code in the repository for reproduction
and explicitly rejected separate repository source-checksum files. Those
instructions supersede the earlier deletion requirement; the closed scientific
findings and historical producing identities are unchanged. Reviewer
`/root/q34_review` independently inspected the restored `experiments/track01`
package, README, workflow/charter wording and narrow supervisor adaptation.

**Accept the reproduction packaging.** All 27 producer files and all seven runtime
oracle modules are byte-identical to accepted producer 05/oracle 01. The complete
local import/build/check path is present and fresh runs do not depend on the
original machine's external raw paths. Repository source-checksum files have
been removed as requested. Git owns repository source versions; generated runs
still retain producing-source snapshots and raw-artifact integrity inventories.

The adapted supervisor removes the prerequisite oracle checksum file and its
metadata field, rejects symlinks, copies the fixed required runtime modules,
and retains copied-source mutation and raw-run integrity checks. Its explicit
supervision-v2 format identifies the changed metadata contract. Independent
synthetic CLI-path checks passed for an oracle directory without a checksum
file, missing-module and symlink rejection before supervision, and mutation of
the copied source during a mocked checker run. These exercised packaging paths
only; no VM or full native oracle was launched. Root also reports passing
existing runner/oracle/supervisor self-checks and a fresh build-only result.

The README correctly specifies repository-root invocation, Python 3.9+, selected
SDK 26.4, external fresh outputs, serial VM/check pairs, the fixed resource limits
and new-host applicability review. Individual commands do not claim to enforce
campaign-wide admission or revive closed execution budgets. Full historical
preparation controls remain external and are distinguished from the runtime
reproduction subset. The revised charter/workflow preserve retirement from
development while permitting this owner-requested code retention. No substantive
packaging defect or new mechanism finding remains; no raw output was deleted
by this preservation step.

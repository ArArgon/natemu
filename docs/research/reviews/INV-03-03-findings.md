# INV-03-03 — Findings review

- **Reviewer and independent context:** `/root/translator`, separate from author `/root/machine`. Reviewer authored INV-03-02 but did not author or edit INV-03-03 or its proposed machine mechanisms.
- **Subject:** source-only interface findings and explicitly provisional placement direction in [INV-03-03](../investigations/INV-03-03.md), September 19, 2026 submission.
- **Verdict:** **accept the scoped source findings and unresolved-blocker assessment; do not accept a machine architecture or Track 03 completion.** No substantive defect in the submitted limited conclusions was found. Open correctness obligations remain architectural blockers, as the author states.

## Material inspected

Read the entire investigation, Track 03 charter, research workflow and relevant INV-01-34 startup findings. Independently opened and checked the primary sources cited by the author:

- [Linux v6.12 boot protocol §1.15](https://docs.kernel.org/6.12/arch/x86/boot.html#bit-boot-protocol-1): 64-bit entry offset, paging, identity mappings, parameters, GDT and interrupt state. This confirms the contract and the distinction from the AP mailbox path; it does not establish an implemented loader.
- [Linux v6.12 virtio-mmio driver](https://raw.githubusercontent.com/torvalds/linux/v6.12/drivers/virtio/virtio_mmio.c): `virtio_mmio_acpi_match` recognizes `LNRO0005`, and the driver has separate conditional discovery paths. An installed module or source file cannot prove initrd availability.
- [Linux v6.12 TSC calibration](https://raw.githubusercontent.com/torvalds/linux/v6.12/arch/x86/kernel/tsc.c): `native_calibrate_tsc` explicitly rejects a non-Intel vendor and insufficient CPUID level, then tests the ratio and frequency. The investigation correctly leaves the CPU-profile/calibration interaction open.
- [QEMU v10.0.0 microvm](https://raw.githubusercontent.com/qemu/qemu/v10.0.0/hw/i386/microvm.c), [virtio core](https://raw.githubusercontent.com/qemu/qemu/v10.0.0/hw/virtio/virtio.c), and [POSIX file backend](https://raw.githubusercontent.com/qemu/qemu/v10.0.0/block/file-posix.c): machine component reuse is supported; ring mapping caches use RCU retirement; `handle_aiocb_flush` tests and latches buffered-write failure. These are adaptation inputs, not evidence that the port's callbacks or host/VM memory ordering preserve them.
- [VIRTIO 1.2](https://docs.oasis-open.org/virtio/virtio/v1.2/virtio-v1.2.html): inspected queue/reset and block-cache clauses, including feature-dependent cache/flush interpretation. The proposed minimal negotiation cannot silently omit the corresponding completion/durability obligations.
- [QEMU security guidance](https://www.qemu.org/docs/master/system/security.html): confirms the stated non-virtualization isolation disclaimer and the lack of a security boundary merely from shared-memory backend subprocesses. The document does not prove the proposed macOS containment mechanism.

## Substantive blockers and their disposition

There is no unresolved blocker **to the narrow source findings**. The following explicit design blockers must remain open in the owning investigation and combined architecture record; acceptance of this review cannot close them:

| Architectural blocker | Consequence / evidence | Smallest next remedy | Closure condition |
| --- | --- | --- | --- |
| Actual stock-kernel transport and boot contract | Linux driver source does not prove target initrd can find/root-mount virtio-mmio; BSP/AP/firmware interaction remains unspecified | Inspect the exact supported image configurations/initrds and firmware discovery/hand-off paths, choosing PCI fallback if needed | Reviewed evidence covers each declared guest image and the chosen boot/transport path, without silent guest narrowing |
| Time and interrupt contract | CPUID/vendor gating and missing clock fallback can invalidate boot; counter sleep behavior is not established | Select a concrete CPU/clock/timer contract and trace discovery, writes, deadline cancellation and waits | Reviewed source contracts and, where needed, fresh evidence establish the chosen behavior and interactions |
| All-writer coherence and device commit | Host writes bypass shadow traps; a bounce buffer alone does not synchronize remapping or invalidate executable aliases | Define the actual pin/commit/invalidate protocol jointly with INV-03-04, including cancellation | No untracked writer, replayed side effect or late completion crosses a retired lifetime |
| macOS containment and promised flush semantics | A subprocess does not restrict same-user host resources; `qemu_fdatasync` naming alone cannot prove the backing-store promise | Inspect concrete enforcement and flush paths for the chosen host backend | Reviewed contract supports declared resource boundary and guest-visible durability/error semantics |

These are already acknowledged by the author, so they are not new review returns. The smallest action after this source review is to resolve one concrete contract rather than treating implementation validation as a substitute for unresolved architecture.

## Adversarial and breadth assessment

The submission preserves required startup32, does not equate GIC signaling with x86 APIC semantics, does not claim TSO covers host queues, and does not convert finite Track 01 fixtures into stock-kernel boot. It explicitly retains queue reset, malformed descriptors, partial I/O, all writers, stale callbacks, host sleep, shutdown, resource exhaustion and peer containment as interactions. UEFI/Windows are appropriately analysis-only; optional lifecycle scope remains explicit work. Cost rankings are qualitative hypotheses and have falsifiers; no timing claim needs qualification because none is made.

The reviewer challenges two tempting shortcuts that the document already avoids: selecting virtio-mmio solely because a driver exists, and calling inherited QEMU devices a containment boundary. Neither shortcut is an accepted conclusion. Copying input data through a bounce buffer also does not itself close the memory-coherence blocker.

## Editorial edits and limits

No direct edits to the investigation. No experiment, replication or executable test was run: this review concerns source contracts and inference only. The detailed Apple containment/time/durability APIs and concrete ACPI shutdown table contents remain unaudited here, matching the author's stated limitation. The review neither establishes all VIRTIO conformance nor a complete source dependency closure of QEMU's device runtime.

## Returns

None required for the scoped findings. The author should link this review, distinguish accepted source findings from provisional machine choices, and replace the completed review next-action with one bounded technical step. Substantive future additions require review in this same thread.

### Return 1 — macOS flush and sandbox source supplements

September 19, 2026, same independent reviewer. Re-read new CLM-03-03-09/10 and
independently inspected QEMU v10.0.0 `util/osdep.c::qemu_fdatasync`, Apple's cited
archived `fcntl(2)` F_FULLFSYNC text, and the installed SDK26.4
`usr/include/sys/fcntl.h` and `usr/include/sandbox.h` declarations/comments.
The QEMU wrapper selects fdatasync/fsync without F_FULLFSYNC; the SDK still names
the drive-flush request. The sandbox declaration requires SANDBOX_NAMED, reserves
other flags and marks the API unsupported/deprecated. These support the narrow
new source findings. The author correctly does not infer modern APFS support,
actual power-loss survival or a working custom sandbox from them.

**Verdict: accept these two source supplements.** The original macOS containment
and actual storage-contract design blockers remain open. No direct authoring
edits or experiment. A future concrete flush or sandbox protocol still requires
its own review here; this acceptance supplies no runtime enforcement guarantee.

### Return 2 — Debian initramfs selection and ACPI autoload supplements

September 19, same independent reviewer. Independently read the downloaded
initramfs-tools 0.148.4 archive without installing/executing it, checked its full
SHA256 against the accompanying official descriptor, and inspected
`conf/initramfs.conf`, `mkinitramfs` and the relevant `hook-functions` definitions.
They confirm MODULES=most dispatch, default base/block classes, explicit
virtio-mmio request and block-symbol selection. `_call_dracut_install` passes
`-o -m`, so absent optional modules need not establish actual inclusion.
Independently opened Linux v6.12 `virtio_blk.c::virtblk_probe`,
`platform.c::platform_uevent`/`platform_match`, and systemd v257
`80-drivers.rules`; their cited selection and modalias routes are present.

**Verdict: accept CLM-03-03-11/12 as conditional source findings.** This narrows
an uncertainty about default construction policy but proves neither an actual
stock image's contents nor successful autoload/boot. The document retains that
limit and the concrete prerequisites correctly. No investigation edit or runtime
experiment; new actual-image/boot evidence still needs its own scoped review.

### Return 3 — exact Debian installer artifacts

September 19, same independent reviewer `/root/translator`: **accept
CLM-03-03-13/14 as scoped artifact/source findings, including the missing block
driver; no boot, device binding or installed-system compatibility acceptance.**
Independently retrieved the versioned official SHA256SUMS and verified it equals
the author's reading copy. Hashed both complete retained files: initrd
`57303d157cffce3fa402301667fbc9b2280aa7372082b081e777375ae49da5f2`, 40,723,557
bytes; kernel `2b2358b37674d2505350528875bb17afae2a36522a9e8a9417eaca65a7da0e08`,
12,142,528 bytes. Both match the correct netboot paths in the versioned manifest.

Using independent in-memory gzip/newc parsing, traversed all 2,511 members to
TRAILER!!! and checked the remaining 192 bytes are zero padding. Did not rely on
the author's inventory or extracted files. Inspected lsb-release, dpkg status,
module paths and decompressed module alias/dependency/vermagic strings. They
confirm installer build 20250803+deb13u7, kernel 6.12.107+deb13-amd64/package
6.12.107-1, MMIO's LNRO0005 alias, network/failover dependencies and entropy
module. Independently read bzImage version/header fields: version, protocol
0x020f and xloadflags 0x007f agree.

Read actual init, start-udev and 80-drivers.rules through the archive's lib →
usr/lib link. The sysfs mount, daemon start and add-event/MODALIAS module-loading
route are present; the named binaries and indexes exist. No virtio_blk-named
member exists, and its name is absent from both module binary indexes. Coupled
with the already-reviewed modular kernel configuration, the document correctly
refuses a stock virtio-root claim. Changing transport to PCI cannot create that
missing block driver. The installer image is correctly treated as an additional
concrete case rather than substituted for installed-system/workload coverage.

No substantive return. No image was installed, modified or executed. This source
artifact review does not validate binary alias-index resolution, live ACPI
resources, executable dependencies, runtime device binding, block-component
retrieval or SMP startup. Those limits remain explicit.


### Return 4 — official storage component and conditional installer retrieval

September 19, same independent reviewer `/root/translator`; bounded to 30 minutes
of source/artifact inspection. **Accept CLM-03-03-15/16 as scoped source findings
and a conditional retrieval/discovery inference.** No runtime boot, storage
binding, full dependency closure or machine-selection acceptance.

Independently parsed the retained official Packages.xz and both udeb ar/tar
archives under
`/Users/liaozping/orca/natemu-raw/track03-source-audit-20260919/machine/`.
Computed whole-file SHA256 and sizes for scsi-modules and disk-detect; they match
the index and claim 15/16 values. The scsi package is standard priority with
Kernel-Version 6.12.107+deb13-amd64 and package version 6.12.107-1. Its actual
virtio_blk.ko.xz decompresses to metadata with `alias=virtio:d00000002v*`, empty
`depends=` and matching vermagic. The package's four dependencies are correctly
distinguished from this module's symbol-dependency list; their full contents
remain unaudited. The index is a retained dated snapshot, not a promise that the
moving trixie index will retain these versions indefinitely.

Independently read anna 1.99's actual choose_modules, install_modules and
retriever_retrieve from the retained source archive. Kernel mismatches are
excluded before selection; matching standard kernel packages receive
want_install. The standard-kernel case is selected even when the general
standard_modules setting is false, but low-memory/user selection and later
failure paths still prevent an unconditional installation claim. Installation
retrieves the index filename, checks package SHA256, and unpacks it. The
investigation properly conditions success on network/configuration/component
availability rather than claiming these source branches have run.

Reparsed the unchanged reviewed initrd to read its actual hw-detect,
net-retriever and update-dev scripts, independently of the author's extracted
copies. Read disk-detect from the separate official udeb. Disk-detect invokes
hw-detect; hw-detect attempts depmod and calls update-dev; update-dev's normal
udevadm path triggers add events and settles. The network retriever constructs
mirror URLs and checks index size/hash against Release. Its authentication has
explicit configuration/tool prerequisites, so this review does not infer a
blanket authenticated or successful retrieval guarantee. The PCI vendor 0x1af4
heuristic is separate and cannot justify ACPI-MMIO selection; the document
correctly uses the standard matching scsi component route instead.

No substantive return. These facts identify an ordinary staged path by which the
missing initial block module can become available. They neither make the initial
netboot archive a standalone virtio-root initramfs nor prove live modalias
resolution, binary/tool dependencies, ACPI device realization, binding or disk
I/O. No downloaded package or script was installed or executed; no guest bytes
were modified. Stop condition reached at the conditional source chain.


### Return 5 — early no-PIC and reference-clock bootstrap

September 19, independent reviewer `/root/translator`: **accept claims 17–18 as
scoped source findings and conditional machine prerequisites.** Independently
read both versions' setup, TSC, PIC, ACPI, delay, PM-header and APIC paths in the
cited v6.8/v6.12 upstream-source reading copies. This is upstream source review,
not a vendor-patch audit or execution of either inherited kernel.

The setup order does place tsc_early_init before ACPI parsing. Default legacy_pic
and quick_pit_calibrate explain why eventual no-PIC firmware does not avoid early
PIT-port accesses. The MADT bit merely bypasses/allows probe; absent PIC requires
probe reads inconsistent with the written mask. Hardware-reduced ACPI installs
different hooks and cannot be assumed equivalent. The investigation correctly
leaves absent-port semantics and bounded calibration failure as concrete design
requirements rather than calling an absent PIT automatically harmless.

FADT extended timer I/O-space selection/fallback and the 24-bit verified early
PM read are present. The five udelay calls do not prove a wall-clock 50 ms interval:
default delay_loop survives failed early TSC setup. Most importantly, both APIC
calibration loops wait on either a calibrated progressing TSC or changing
jiffies; PM readings occur in the sampled handler and subsequent calibration.
The zero-tsc_khz/no-tick/advancing-PM counterexample is valid. A readable PM counter
alone cannot bootstrap this polling loop.

No substantive return. This establishes dependencies for a candidate no-PIT
machine, not timer functionality, firmware compliance, successful calibration,
interrupt delivery or stock boot. Existing package CONFIG_X86_PM_TIMER evidence
is reused only within its previously reviewed identity scope. No source or guest
bytes modified and no runtime experiment.


### Return 6 — reused legacy clock objects and provisional composition

September 19, independent reviewer `/root/translator`: **accept claim 19's
scoped source observations and claim 20's provisional adaptation rationale.**
This accepts the reason to prioritize a composition, not an accepted machine,
working host integration or firmware/interrupt contract.

Independently inspected the cited QEMU v10.0.0 microvm, PIT/PIC/ISA, PC speaker,
RTC and PC/ACPI construction reading copies, plus Linux v6.12 RTC and PIT code.
Microvm indeed constructs software legacy ISA objects alongside virtio-mmio.
The software PIT uses a virtual-clock timer and ISA I/O region; the public
initializer provides IRQ wiring. This supports existing host-model reuse but
not independent extraction from its QOM, timer and machine dependencies.

Port 0x61 is substantive: pcspk reads channel-2 output/gate and sets its gate;
realization registers I/O independently of audio registration, and voice use is
guarded. PC explicitly links speaker to PIT, whereas the shown microvm PIT call
alone does not. The proposed headless register model therefore has direct source
support. RTC uses the configurable rtc_clock, calendar initialization, timers
and ISA routing; Linux's CMOS access and conditional platform registration are
present. The document correctly refuses to equate these clocks with the shim
TSC or claim a working wallclock from source alone.

Conventional PC FADT construction supplies PM I/O fields and RTC century data
from selected machine objects. Microvm uses hardware-reduced ACPI instead; the
candidate cannot combine that FADT blindly with conventional PM assumptions.
Linux's PIT clockevent initialization supports the suggested bootstrap route,
subject to live clock/IRQ routing and firmware/direct-boot initialization.

No substantive return. Preferring already implemented register/timer behavior
over custom absent-device bootstrap semantics is a defensible provisional
engineering judgment, not a measured cost result. The next provider/IRQ/ack
composition pass remains necessary, including PIC/IOAPIC duplicate-delivery
avoidance, SCI, direct-boot initial state, cancellation and clock coherence.
Whole-PC substrate reuse remains a legitimate fallback if selecting a smaller
bundle adds more unproved semantics. QEMU v10 device sources and the separately
pinned translator are distinct inspected identities; this review proves no
cross-version build or ABI compatibility. No source was executed or changed.


### Return 7 — PIIX provider, direct initialization and interrupt seams

September 19, independent reviewer `/root/translator`: inspected the cited QEMU
v10.0.0 PIIX4/ACPI/PIIX/IOAPIC/PC builder and linker sources, SeaBIOS rel-1.16.3
PCI/romfile-loader sources, and Linux v6.12 ACPI sleep/SCI/root-pointer consumers.
**Accept source claims 21, 22 and 24; claim 23's source facts and provisional
bundle rationale are acceptable subject to the small clarification below.**
No complete machine, loader, interrupt protocol or boot result is accepted.

Confirmed PMBASE alignment/config0x80 gating and reset-disabled decode, the
separate no-SMM acpi_only/SCI_EN behavior, and SeaBIOS's explicit PCI writes.
These distinguish two independent prerequisites and justify initialization after
reset but before final ACPI snapshot. Confirmed matching _S5 type zero and PM1
SLP_EN shutdown-request path; Linux writes the legacy sleep controls. This
accepts the interface chain, not filesystem flush or lifecycle completion.

Confirmed SCI9 wiring, IRQ0-to-GSI2 mapping/MADT override, conditional PIC
presence, Linux SCI defaults, IOAPIC remote-IRR/EOI behavior and MSI-format output.
Confirmed the separate ordinary PIC acceptance check before pic_read_irq. Retaining
host IOAPIC is a reasonable provisional way to retain those state machines,
provided the cross-boundary request/acknowledgement/EOI ownership is explicit.

**Return R7-1 — clarify eager ExtINT acknowledgement.** In the inspected
ioapic_service, ioapic_entry_parse is called before the !info.masked check;
parse itself calls pic_read_irq for ExtINT. Thus the named parse helper is not a
pure decoder, and host reuse alone cannot imply that acknowledgement waits for
final LAPIC admission. Consequence: an adapter that speculatively parses or
replays a delivery request can consume PIC state before accepted delivery.
Smallest remedy: record that source call order and retain an explicit requirement
for a single owned acknowledgement phase in the forthcoming bridge contract.
No new experiment or device reimplementation is requested. Closure is an author
wording correction and reviewer recheck, not proof of the whole bridge.

The linker sources emit allocation/relocation/checksum commands rather than
finished guest tables; SeaBIOS consumes those classes. Linux boot_params has the
RSDP handoff. Direct finalization must preserve the entire chosen table graph
and object/configuration snapshot, including virtio-MMIO additions. The proposed
provider is a design direction, not a claim that ordinary PC firmware/board
initialization can run unchanged at the EL1 direct-boot boundary.

No runtime execution and no author-file edits. Pending R7-1 only concerns the
bounded wording/semantic seam; all stated implementation/coverage limits remain.


**R7-1 closure, same reviewer/date:** re-read the corrected claim23 paragraph.
It now states ioapic_service → ioapic_entry_parse → pic_read_irq ordering before
the mask test, rejects an inferred post-admission acknowledgement guarantee,
and makes speculative/double-ack prevention an explicit bridge obligation.
This satisfies the requested source/interpretation distinction. **R7-1 closed;
accept claims 21–24 and their provisional provider/placement rationale in the
stated source/conditional scope.** No unresolved findings return in this review.
Bridge correctness, coherent version integration, complete table/device state
and machine acceptance remain future design work; no new source pass occurred.


### Return 8 — coherent selected revision and changed locking seam

September 19, independent reviewer `/root/translator`: **accept claims25–26's
scoped rechecked provider facts and provisional single-revision selection.**
Independently read the named PC/PIIX, ACPI, timer, speaker, RTC, PIC/IOAPIC and
linker seams in the fresh c1c18d1e640b64292859ce9f30f3c344edfb0294 copies and
compared the relied-on interfaces with the previously reviewed v10 sources.
The scope is explicit symbol/contract continuity, not every source change or a
linked build. Linux and SeaBIOS remain separately versioned evidence.

PM decode/base/reset and no-SMM ACPI-only behavior, FADT/DSDT shutdown fields,
SCI9, GSI/IRQ0 mapping, PIC acknowledgement, remote-IRR/EOI and table-linker
contracts remain present. The ExtINT parse-before-mask caveat remains; the new
revision does not close it. Host IOAPIC output now uses address_space_stl_le and
its registers explicitly select little-endian behavior. The speaker typed PIT
link/missing-link check and optional audio backend preserve the proposed
headless register seam. RTC and PIIX changes are appropriately not called
bit-identical behavior or migration compatibility.

Confirmed PM timer calls memory_region_enable_lockless_io; the memory API
specifies BQL-free accesses and its implementation disables the region's use of
the device-scoped reentrancy guard. Author was asked for that minor scope wording
precision. This creates an explicit access/lifetime/clock-serialization premise,
not proof of unsafe upstream behavior or of safe bridge concurrency. The
provisional initial serialization option is coherent but still needs its host
owner protocol.

No substantive findings return. Selecting one pinned QEMU source runtime avoids
an unjustified mixed-QOM assumption; it does not prove configuration/build closure,
loader correctness or the host-IRQ/shim-LAPIC interface. No runtime execution or
author-file changes in this review.

## Return 9 — claims 27–28, September 20, translator reviewer

**Accept the scoped source distinctions and conditional ownership interface.**
I independently inspected the five pinned IRQ source copies and retained Intel
253668-092US §§13.5.1/13.8.4: ExtINT bypasses IRR/ISR; LINT ExtINT has its own
trigger semantics. `pic_read_irq` performs acknowledgement, including cascade
and spurious handling. `apic_eoi` clears ISR and conditionally broadcasts level
EOI, while IOAPIC can reoffer a still-asserted line. The submitted distinction
between fixed delivery, external IACK and PIC EOI is necessary.

Confirmed `ioapic_entry_parse` eagerly acknowledges ExtINT before the service
mask check; its route-update caller also parses. Confirmed the bus ExtINT branch
falls through to `apic_set_irq`, unlike local LINT ExtINT. Replacing the parser
and auditing all enabled callers is therefore a concrete adaptation obligation,
not an optional message-format change. The record correctly leaves IOAPIC ExtINT
bookkeeping and complete arbitration unresolved instead of accepting upstream
reuse as their proof.

The proposed single owner and pre-callback RAM grant remove the sampled cycle
where a callback waits for quiescence while the paused vCPU awaits its reply.
Borrowing existing DRAIN_SERVICE authority must remain distinct from requesting
a new grant. An EOI reply cannot wait for guest acceptance of its consequent
offer. The retained IACK reservation prevents duplicate acknowledgement and
freezes the relevant guest boundary, while architectural delivery faults after
IACK do not undo or repeat it. These are coherent interface rules, conditional
on callback reachability, reserved completion capacity and nonblocking offer
publication; they do not prove all callbacks already obey them.

The local reservation must still service native coordination and control replies;
it cannot become a mutex held across a host wait or a promise that another guest
vCPU runs. Source/controller effects must remain durable when transport is full;
reserved reply space alone is not proof of a bound for every producer. Those
are explicit premises for the forthcoming complete queue/lifetime contract,
not a substantive return against this bounded claim. Logical/lowest-priority,
changed routing, reset and every enabled delivery mode remain required coverage.
No full controller/machine acceptance, runtime execution or author-file edit.

## Return 10 — claims 29–30, September 20, translator reviewer

**Accept the scoped clock source and conditional selected policies; no full
machine acceptance.** I independently read SDK 26.4 `hv_vcpu.h` lines 438–449
and `mach_time.h`: the explicit CNTVCT/mach_absolute_time equality is present,
and continuous time is distinguished as advancing during sleep. The saved Apple
API page identifies CNTVOFF but does not supply the stronger equality; the author
attributes that correctly to the SDK. Direct substitution with the recorded
successful offset supports the common-reference argument. It is not empirical
proof of timer delivery latency or every host generation.

The active-time policy is an explicit revision of the earlier sleep-inclusive
hypothesis. HLT/preemption must continue VM time; only an explicit completed
whole-VM pause freezes it. Capturing the epoch after quiescence and publishing
pause accumulation before release avoids a cross-vCPU torn epoch, conditional
on the stated coordinator authority. Timer-arm/lifetime checks prevent a late
native callback from manufacturing expired guest time. Per-vCPU TSC write offsets
are separate from VM time and native counter offsets, so they cannot accidentally
retime PIT/PM/RTC or peers. The fixed frequency, deadline profile and precise
rounding/wrap rules remain named interface obligations, not inferred from the
SDK equation. An ordinary memory maintenance pause must not silently become a
VM-time freeze merely because a vCPU temporarily parks.

The storage policy correctly separates writeback completion, a successful
F_FULLFSYNC media-flush request, filesystem consistency and directory publication.
Its barrier must cover preceding admitted writes and backend completions for the
selected disk; serialization of later requests is a valid initial ordering rule.
Returning errors instead of substituting weaker flush success is coherent.
Asynchronous flush/stop completion preserves the host owner's no-blocking rule;
forced process termination is not a rollback or invented successful completion.
The previously reviewed device-compliance caveat remains applicable. No new
hardware durability evidence follows from selecting this contract.

Process separation alone does not establish the stronger per-VM file, signal,
network and supervisor-authority restriction. The App Sandbox primary description
supports an entitlement boundary but does not prove a shared helper/container
configuration provides distinct VM capabilities. Keeping that mechanism as the
first unresolved authority choice is justified. It must be resolved by a concrete
enforceable construction or precisely scoped missing behavior, not by requiring
a complete implementation before architecture acceptance.

No substantive return. Full IRQ modes, callback/queue retention bounds,
containment mechanism and remaining CPU/event semantics are still open; naming
them in a whole-machine table does not close them. No VM, sandbox or durability
experiment was performed and no author file was changed by this review.

## Return 11 — inventory cleanup consistency, September 20

Translator reviewer; editorial review only, no new source research. **Accept the
cleaned inventory in scope.** The fixed host-codegen/captive-runtime boundary,
charter questions 1/8–12, common-pin dependency scope and source-versus-component
choice distinctions are explicit. Claims 01–30 and their source/review provenance
remain; historical pass work orders are subordinate to one current work status
and an unstarted bounded authority decision pass. Clock, IRQ, device, boot and
containment gaps remain visible rather than being marked accepted by cleanup.

One wording return was made and closed: claim 20's clock-island comparison
initially still implied reduced total semantic work from existing device models.
The author now calls this an unverified placement/cost hypothesis, keeps both
legacy and no-PIT total costs unranked, and labels conventional PC code a component
provider/reference rather than an end-to-end fallback. I rechecked the correction.
The concrete source-derived calibration obligations remain useful; they do not
select a code generator or prove cheaper captive integration. No outstanding
cleanup return; this does not close the substantive machine choices.

## Return 12 — per-VM authority source pass, September 20

Reviewer `/root`, independently inspecting the machine agent's claims 31–33.
**Accept their source/conditional scope after the control clarification below.**
This accepts the identified API route, candidate authority sequence and precise
remaining platform question. It does not accept an enforced containment policy,
signing combination or experimental method ready for execution.

I read Apple's current helper-embedding, HVF-entitlement, Hypervisor-overview and
network-client texts, its file/container documentation and archived inheritance
reference. The shared static-rights and code-identity container contracts do not
supply instance-specific VM isolation. The separate per-process HVF requirement
and explicit sandboxed-process support justify neither a blanket incompatibility
nor a claim that this particular inherited helper is correctly entitled. The
candidate appropriately keeps independently configured XPC as an alternative.

I independently read the local primary Chromium sources at the declared
`5233c110e9d303cd8c144e8c4e0591147ec53880` pin: `seatbelt.cc`,
`sandbox_serializer.cc`, `seatbelt_exec.cc`, `seatbelt_unittest.cc`, `README.md`,
`seatbelt_sandbox_design.md` and the relevant `common.sb` rules. Source policy is
applied with flags zero and errors are propagated. The SDK 26.4 header I checked
instead documents named profiles and rejects reinitialization; the custom API
therefore remains an explicitly undocumented dependency. The inherited-write/
ftruncate and self-process-info cases are test source expectations, not observed
results here. They support inspecting retained descriptors and explicit operation
defaults rather than interpreting default-deny as a complete capability proof.
The V2 initializer/Mach-port rationale supports the minimal bootstrap sequencing;
it does not establish our Hypervisor framework's actual resource dependencies.

The conditional authority argument includes descriptors, Mach/bootstrap rights,
shared memory and initializer-acquired resources, and does not confuse path
denial with revocation of a preopened disk. The per-VM channel cannot turn into
an ambient supervisor service. These are necessary candidate premises, not a
finished manifest or reason to import Chromium's full allowlist. Creation before
restriction cannot prove later map/run/timer/destruction success. Full-sync and
connected-channel behavior also remain explicit checks under the actual policy.

**Return resolved:** the initial smallest-resolution paragraph named an
unrestricted control mainly for HVF/signing. I required that control for every
challenged operation, especially peer task/control access already denied by
ordinary OS protection. The author now requires matched identity/resources and
labels baseline-denied attempts as non-evidence for custom policy enforcement;
an otherwise permitted controlled target or an unestablished policy claim is
required. I rechecked that correction. No substantive return remains.

No program, sandbox, VM or signing operation was run. I used the cited local
primary copies and independently retrieved Apple file-access/inheritance text
and Chromium README through the web; JavaScript/Markdown retrieval limitations
were resolved for the overview by reading the author's direct Apple text copy.
I make no independent download/byte-match claim for every Chromium file. The
next step must make the exact profile and finite platform method reviewable;
this acceptance does not turn the source pass into runtime decision evidence.

## Return 13 — stopped containment control and inherited launch mismatch

September 20, reviewer `/root/memory`; read-only findings check of
`containment-reviewed-01` and claim 34. **Accept the factual stopped-run finding
only.** No containment result, successful native guest execution or identified
root cause is accepted. No probe, signed binary, guest or VM was executed in
this review.

I independently recomputed all **33** entries in the retained
[SHA256SUMS](/private/tmp/natemu-track03-raw/containment-reviewed-01/SHA256SUMS),
including the producing sources and raw outputs; all match. The
[manifest](/private/tmp/natemu-track03-raw/containment-reviewed-01/manifest.json)
runner/retention hashes also match their snapshots, and its copied control
output equals the raw stdout. Host metadata records Mac16,10, macOS 26.6.2
(25G83), SDK 26.4. There is exactly one cell, `0-control`, with `timed_out=true`,
return code -9 and overall `failed_or_inapplicable`. The retained runner kills
and waits for the process group after its 20-second communicate timeout and
stops the matrix on the invalid control.

The [control output](/private/tmp/natemu-track03-raw/containment-reviewed-01/0-control.stdout)
supports the stated preceding file/channel/peer/network and HVF-configuration
returns. Peer stdout contains its PID and S marker. `task_for_pid` returned 5
in the control, so it cannot validate custom-policy task exclusion. The last
flushed operation is `timer_mask_value`; in the retained
[probe](/private/tmp/natemu-track03-raw/containment-reviewed-01/source/probe.c:74)
the next operation calls `hv_vcpu_run` before logging its return. No such return,
guest HVC/result or teardown record exists. This localizes the uncompleted
run-call interval; it does not diagnose the mechanism. No BEFORE/AFTER cell ran,
and the control branch does not call `restrict_worker`/`sandbox_init`.

**Inherited-premise mismatch:** retained
[run.py](/private/tmp/natemu-track03-raw/containment-reviewed-01/source/run.py:22)
sets `RLIMIT_CPU=(10,10)` in `child_limits`, which is actually passed as this
worker's `preexec_fn`. Build-command CPU limits of 60 in the manifest do not
describe that worker setting. The worker cell lacks independent native
`getrlimit` readback, so this finding is about the producing launch code, not
an invented measured limit field.

[INV-01-30](../investigations/INV-01-30.md:480) already records the one-variable
CPU-limit-setter omission diagnostic: the inherited-unlimited run reached HVC
and was reviewed, after which the foundation runner refused non-unlimited
inherited host limits. The [frozen README](../../../experiments/track01/README.md:43)
also states the unlimited inherited CPU-time requirement. Thus this run did
not preserve that known applicable launch precaution. The Track 01 finding
itself names a candidate harness/host dependency, not a universal HVF rule;
CPU10 here versus CPU60 there and other fixture differences prevent attributing
this timeout to the setter without a fresh controlled diagnostic.

The owning result must retain this mismatch alongside its failed-control
interpretation and reconcile its next diagnostic with that inherited evidence.
A separately reviewed one-variable launch diagnostic can test the candidate
explanation; this findings check authorizes no retry and predicts no result.
Successful setup calls and the preserved failure remain useful facts, but neither
the custom sandbox nor the guest shim is accepted or rejected by this run.

## Return 14 — successful unrestricted CPU-limit diagnostic

September 20, reviewer `/root/memory`; read-only independent review of claim 35
and `containment-cpu-limit-control-01`. **Accept the narrow observed comparison.**
No restricted-worker, containment or platform-wide RLIMIT_CPU conclusion follows.
I did not execute or replicate a probe in this review.

All **33** retained
[checksum entries](/private/tmp/natemu-track03-raw/containment-cpu-limit-control-01/SHA256SUMS)
were independently recomputed and match. The
[manifest](/private/tmp/natemu-track03-raw/containment-cpu-limit-control-01/manifest.json)
runner/retention digests match their snapshots and the copied output equals raw
stdout. Compared byte-for-byte with `containment-reviewed-01`, retained `probe.c`,
`worker.sb`, `entitlements.plist` and `retention.py` are unchanged. The runner
diff adds control-only bounds/metadata and the inherited-unlimited guard, and
omits the CPU setter for the VM worker while preserving its other pre-exec
limits/session setup and 20-second wall timeout. The peer still uses CPU10;
build commands still use CPU60. The shortened whole-session budget and single
cell selection are diagnostic bounds, not changes to guest setup.

There is exactly one unrestricted control cell, return code zero and no timeout.
[Raw output](/private/tmp/natemu-track03-raw/containment-cpu-limit-control-01/0-control.stdout)
records successful `hv_vcpu_run`, exception syndrome decimal 1509949440
(`0x5a000000`, EC `0x16`) and X0=66 (`0x42`). The declared HVC/result oracle and
the listed unmap/remap/unmap, vCPU destruction and VM destruction calls succeed.
The file marker, full-sync return, socketpair exchange, peer-file open, observed
peer signal and loopback conditions also match the control predicate. Task-port
acquisition remains baseline-denied with error 5; timer configuration is not
timer delivery evidence.

The manifest's two unlimited values come from the parent's recorded inherited
limit and are copied into the cell. They are **not** an independent native-worker
`getrlimit` measurement. The retained call uses `child_limits(False)`, so the
producing code supports the stated setter omission without inventing a readback.
The successful one-variable worker-launch comparison supports the inherited
launch-condition hypothesis on this fixture/host. It does not prove the OS
mechanism or exclude all uncontrolled run-to-run factors. Neither compared run
calls the custom-policy initializer in its control branch. This diagnostic's
one-run scope is exhausted; authorization and findings for any subsequent full
matrix are separate from this verdict.

## Return 15 — restored matrix and independent replication

Reviewer `/root`, separate from probe/result author `/root/machine` and
replication investigator `/root/memory`. **Accept claim 36 in its stated finite
observational and conditional-inference scope.** No substantive return remains.
This does not accept the complete production containment boundary.

I inspected both retained manifests and raw cell outputs, independently
recomputed all 48 checksums in each run and compared the five producing inputs.
The replication used a fresh retained build with byte-identical C, profile,
entitlements, runner and supervision dependency. Its preparation was checked
under pre-execution Return 4 before the independent investigator ran it. I also
checked C/profile/entitlement identity with the failed first control, and the
same corrected runner as the successful diagnostic. Neither failed artifacts
nor the baseline task denial are excluded from the record.

Across the two matrices, twelve cells exited zero without timeout. I checked
raw sandbox status, expected HVC syndrome/X0, successful HVF operations including
destruction, channel bytes and signal observations, rather than relying only on
the final classifier. Each matrix has successful peer-file, actual signal and
loopback controls followed by EPERM on those operations in all four restricted
cells. The replication's operation assessment matches the original. File and
socketpair use through the deliberately inherited capabilities stays distinct
from acquisition of new peer resources. Both setup orders preserve this finite
native fixture's functionality under the exact profile.

Task acquisition returns error 5 and no right even in the unrestricted controls;
it cannot establish custom-policy task or memory isolation. Timer configuration
and full-sync API returns cannot establish delivery or power-loss behavior.
The observations are one-vCPU native fixtures on the recorded M4/OS/SDK, not
translated x86 or SMP evidence, and port-number diagnostics are not an audited
capability manifest. The statement of these limitations is adequate.

The initialization inference is justified in that scope: the tested HVF path
does not need an unrestricted warm-up phase. Applying restriction before heavy
initialization is therefore a supported direction for this candidate, consistent
with claim 32's initializer-capability concern. This does not preaccept future
library grants, signing arrangements or backend operations, nor rank codegens.
The production authority manifest and remaining peer-memory/Mach-service
argument stay open. Both matrix budgets are exhausted; acceptance is not a
standing authorization for more runs.

## Return 16 — worker authority construction, September 20, root

**Accept claims 37–38 in their stated source and conditional design scope.**
Root independently inspected Apple's pinned XNU `kern_proc.c` CONTROL/READ
acquisition checks, `mach_vm.defs` read/write map types, `ipc_tt.c` inheritance,
conversion and special-port branches, `kern_exec.c` NULL-port/spawn handling,
and the installed SDK declarations. The separate READ path and already-held
rights are concrete gaps in extrapolating from the earlier CONTROL denial.
The record correctly distinguishes this public release's source from the
shipping OS binary and its undisclosed policy implementation.

The explicit per-VM resource set and rejection of application peer-capability
exchange are coherent interface choices. Initial rights and later supervisor
grants must be bounded by construction, independent of a compromised worker's
cooperation. A byte channel alone is not a sandbox: the trusted endpoint must
enforce its fixed resource/lifecycle authority and never treat ancillary rights,
worker-selected paths or PIDs as authorization. The submission states this
boundary and keeps the network endpoint and production backend requirements open.

The proposed launch context remains conditional. NULL bootstrap does not clear
registered or exception rights, and the existing task-access port cannot simply
be overwritten. The claim identifies these distinctions and does not label
unidentified inherited rights harmless. Claim 36 did not exercise this sanitized
launch or hardened target signing, so no such observation is inherited. Separate
READ acquisition protection remains unresolved even if CONTROL is denied.

No substantive return remains for these scoped claims. Accepting the capability
interface does not accept its platform enforcement, the complete worker manifest
for unselected backends, or question 12 as a whole. The next method-preparation
pass is bounded and includes the unresolved launch premise; any later runtime
still requires its own reviewed method and controls. No additional experiment
was executed or authorized by this findings review.

## Return 17 — independently replicated sanitized launch, September 20, root

**Accept claim 39 in its finite observed scope and conditional initialization
consequence.** Author run `containment-launch-reviewed-01` and independent
memory-agent rebuild `containment-launch-replication-01` each completed the three
reviewed cells. Root checked all 47 hashes for each run, six producing-input
identities against reviewed preparation and between runs, matching assessments,
and raw launch/guest/task-query records. Root separately checked the raw HVF
returns, file marker/full-sync, socket response, peer-operation outcomes and
observed signal markers rather than relying only on the assessment labels.

Both runs show bootstrap and registered ports present in ordinary launch and
absent in sanitized launch. Task/main-thread exception counts were zero in all
cells: this proves the required observed postcondition, not removal of nonzero
handlers. The sanitized worker successfully applied the unchanged restriction
and performed the finite HVF/native guest/resource operations. No additional
permission, different guest or CPU-limit setter was used. These results support
removing the tested bootstrap/registered channels before this minimal worker's
restricted HVF initialization on the recorded M4/macOS/SDK/signing setup.

Each unrestricted control successfully performed peer file open, observed signal
and loopback connection; both restricted cells denied those operations. Both
task acquisition flavors were denied already in the unrestricted controls, so
the record correctly supplies no custom-policy READ/CONTROL exclusion evidence.
No peer memory read/write occurred. The one-vCPU native fixture establishes
neither translated x86 behavior nor SMP, timer delivery, physical durability,
complete inherited-right exclusion or all production-library initialization.

The load-bearing finite launch observation has independent rebuild replication
under separately reviewed preparation. No substantive return remains for claim
39. The unresolved production READ/CONTROL protection and actual backend grants
stay with question 12; neither this verdict nor successful postconditions close
the complete containment decision. Original and replica run budgets are exhausted.

## Return 18 — target-independent acquisition guard, September 20, root

**Accept claims 40–41 as scoped source facts and a conditional two-entry design
choice.** Root independently inspected the retained XNU trap/syscall tables,
Arm64 Mach and BSD dispatch checks, MAC hook and installed Apple profile examples.
Trap 45 and BSD 539 name the stated APIs; the inspected filter hooks precede
their implementations and receive process/call identity. This supports the
conditional argument that a bound call-number denial cannot be evaded by changing
the target PID. It does not demonstrate that the unexecuted profile text installs
that filter on the current OS. Shipped examples establish vocabulary/use; the
private implementation and exact shipping-kernel identity remain qualified.

The submission explicitly retains debug-control, NAME/INSPECT derivation,
identity-token, held-right and ptrace obligations. Thus accepting this narrower
choice does not claim a complete list of acquisition routes. Self-target calls
are suitable prospective positive controls for target-independent denial, but
the eventual method must distinguish effects of the original profile from the
new rules. If the original profile already denies a self query, a denied query
under the amendment alone cannot establish the added filter's binding. Preserve
that as an inconclusive attribution or use a separately reviewed discriminating
control; do not silently credit the amendment.

Root also checked the Apple library-validation/JIT contracts and the existing
probe's host RW versus guest EXEC mapping. Host generation does not intrinsically
need host executable mappings or a library-validation exception. Actual imported
allocators/initializers and release signing still need their named conformance;
no production Hardened Runtime experiment is inferred. The same-Team-ID/Apple
dependency direction is a conditional signing choice, not a READ protection proof.

No substantive return remains for these source/conditional claims. Method and
code preparation are the bounded next action, with the attribution control above;
no sandbox execution, full authority acceptance or additional run budget follows
from this findings verdict.


## Return 19 — two-entry attribution stop, September 20, root

**Accept claim 42 only as the retained finite baseline observation and
method-required inconclusive stop.** Root inspected the schema-v2 manifest,
both worker outputs and assessment, verified all 45 retained checksums and
all seven producing inputs against reviewed preparation 03. Unrestricted
self CONTROL/READ succeeded through both API and direct entries. The original
profile denied both CONTROL calls with Mach error 5 while both READ calls
succeeded. The checked native HVF and own-resource controls passed; neither
worker timed out. These results match the author's table and the reviewed gate.

Only two cells ran. The amended policy was never applied, so there is no
amendment binding or enforcement result, successful or failed. The runner's
zero exit denotes an intentional retained stop. Self-target observations do not
establish peer access or complete acquisition-route exclusion. No replication
is required to refrain from making those unsupported claims; this acceptance
does not promote the stop into replicated architectural evidence.

The combined all-success baseline is unavailable for this unchanged method on
this fixture. Accept the consequence to avoid an unchanged rerun. The next
action may prepare a discriminating per-flavor method with a separate review
and explicit execution budget; this verdict authorizes no additional run.
Complete containment and the conditional two-entry guard remain unresolved.


## Return 20 — READ-only attribution, September 20, root

**Initial findings review: claim 43 accurately reports the original run;
load-bearing acceptance awaits independent rebuild replication.** Root inspected
the raw three-cell outputs, schema-v3 manifest and assessment, verified all 49
checksums and eight inputs against reviewed preparation 02. Both self READ
paths succeeded under unrestricted and original-profile controls, then returned
exactly minus one/EPERM/no right under the sole BSD-539 amendment. Both CONTROL
paths retained Mach-5/no-right results in the restricted cells. Native HVC/X0
and required worker/resource/launch controls passed; no worker timed out.

This is a discriminating finite policy comparison. The author correctly does
not credit existing CONTROL denial to a new rule or substitute self access for
an observed peer-access test. The pre-dispatch source inference and other
acquisition routes retain their separate qualifications. No broad containment,
production signing, durability or timing claim follows. The original run budget
is exhausted; replication needs its own reviewed preparation and budget.

### Return 20 replication report — September 20, memory

Executed the one separately authorized independent fresh rebuild/three-cell
replication, with no source changes or retry:
`/private/tmp/natemu-track03-raw/containment-read-filter-replication-01`,
2026-09-20 09:23:19–09:23:22 UTC. Used `--read-filter --execute`, the retained
schema-v3 runner, unchanged 100-second phase/20-second worker caps and inherited
unlimited VM CPU limits with no setter. The supervised session completed with
runner exit zero; all three sanitized workers exited zero without timeout.

Independently verified all **49** replica file hashes and all **eight** producing
inputs byte-for-byte against `containment-read-filter-reviewed-01/source`.
The fresh runner built from its own retained snapshot. Manifest SHA-256:
`2675a8db48487b886c7cd730acb19606255e7a80c2d7c4b85a0c0c3bd8266d36`;
SHA256SUMS file SHA-256:
`d24216d8c41e20712c3b273c56a21d25079234486c4f864e26582550fc752594`.

Raw outcomes reproduce the original distinction:

| Sanitized cell | Self CONTROL API/direct | Self READ API/direct |
| --- | --- | --- |
| Unrestricted | Both success, valid rights | Both success, valid rights |
| Original profile BEFORE | Both Mach 5, no right | Both success, valid rights |
| READ-only amendment BEFORE | Both Mach 5, no right | Both -1, EPERM (1), no right |

All four tested launch-state counts were zero in every cell. Self-task facility,
HVF setup/run/expected HVC with X0=0x42/destruction, own-file/full-sync and channel
checks passed. The retained assessment is
`read_denied_after_added_rule_control_unchanged`; manifest status remains
`read_filter_collected_requires_findings_review`. That status is appropriate:
this is the replicator's raw report, **not self-acceptance of the replica**.
Root must independently verify the artifacts and make final scoped disposition.

This repeats only the finite self-target/API/direct READ comparison on the named
host. It does not turn unchanged CONTROL denial into new-rule attribution, prove
peer protection, test all acquisition/held-right routes, or establish full
containment, timer delivery or storage durability. The replication budget is
exhausted; no further run is included.


### Return 20 closure — independently rebuilt READ comparison

**Accept claim 43 in its finite observed scope.** Memory independently rebuilt
and ran `containment-read-filter-replication-01` under the separately reviewed
preparation, September 20 09:23:19–09:23:22 UTC. Root independently verified all
49 replication hashes, eight input identities against the original run, the
three raw worker outputs and their launch/resource/native guest checks. Both
READ paths again changed from successful original-profile controls to
minus-one/EPERM/no-right results under the sole BSD-539 amendment. CONTROL
remained Mach 5/no right in both restricted cells, self task facility remained
usable, and every worker exited zero without timeout.

The original and independent rebuild agree on this exact M4/macOS/SDK/signing
fixture. This accepts the narrow READ rule attribution and sampled compatibility.
It supplies neither an observed successful peer-access control nor complete
acquisition/derivation exclusion, and does not test a new CONTROL rule. The
source-based pre-dispatch argument retains its platform/version qualification.
No substantive return remains for this finite finding. Both execution budgets
are exhausted; remaining guard coverage and production authority stay open.


## Return 21 — alternate CONTROL entry, September 20, root

**Accept claim 44 as scoped source facts and a conditional explicit guard.**
Root independently read the retained Mach table, debug-control handler, Arm64
pre-dispatch filter and task-flavor conversion routine. Trap 96 is a distinct
entry. The ordinary non-private-entitled handler path still consults CONTROL
MAC/task-access authorization; entitlement absence alone does not exclude it.
The existing profile may already deny it, so this is an unclosed protection
argument, not an observed bypass. BSD-539 evidence cannot establish the Mach
filter's binding. The proposed exact trap guard has the stated target-independent
consequence only if that binding holds on the applicable platform.

The conversion switch permits stronger capabilities to serve weaker requests;
NAME/INSPECT are not READ capabilities through this converter. Already-held
READ/CONTROL rights bypass PID reacquisition, so launch and no-transfer premises
remain essential and not exhaustively proved by sampled empty ports. No broader
identity-token or authority acceptance follows.

No substantive return remains for this narrow choice. Pursuing its binding is
a bounded next action, not a dependency blocking independent examination of
other routes. Preserve the lack of observed exploit and the shipping-kernel
qualification in the current summary. No new runtime budget is granted.


## Return 22 — Mach-96 handler-entry discriminator, September 20, root

**Accept claim 45's scoped source inference and method direction.** Root
independently inspected the exact handler and Arm64 dispatch branches. A null
target port with a valid output address reaches the handler's null copyout and
Mach-5 return before target-task authorization. The inspected pre-dispatch
rejection bypasses that copyout. Distinct seeded words therefore supply an
observable predicted effect beyond an already-denied return code, without
acquiring a debug-control right. The actual unrestricted and original-profile
controls remain mandatory; source is not a measurement of the shipping kernel.

The sole-rule delta, exact return/word checks, two sentinels and positive worker
controls make the proposed attribution discriminating. Wrong errors, pointer
failures, unchanged baseline sentinels or process death cannot become a pass.
The valid-target inference remains conditioned on applicable call-number filter
semantics, not observed peer access or complete capability exclusion.

Clarify the method's “no peer query” wording before code preparation: the reused
probe already contains peer-resource/task diagnostics. The new discriminator
must make no peer debug-control or valid-target debug-control call; any retained
older diagnostics keep their existing separate scope and controls. This editorial
clarification does not add authority or support a new peer-access claim.

Accept bounded code/oracle preparation, not executable readiness or execution.
The new assembly, cell-identity gates, profile delta and retained build must
receive independent pre-execution review. No runtime budget follows here.

## Return 23 — Mach-96 negative/inconclusive result, claim 46

**Independent root review: accept accurate reporting of the finite result, not
Mach-filter attribution or an architectural rejection.** Root independently
verified all 52 retained hashes and nine input identities against preparation 01,
replayed the retained classifier and checked all three worker outcomes and raw
sentinel records. The 10:01:54–10:01:56 UTC run completed without timeout; all
required launch and positive-operation checks pass. Both baseline cells return
5 and zero both output words. The amended cell returns 53 and preserves both
seeded words; pre/post writable-word checks pass.

The predeclared exact-5 acceptance condition is not met. The assessor correctly
returns `mach96_filter_or_compatibility_failure_no_attribution`; neither zero
worker exit status nor preserved sentinels overrides that criterion. No positive
policy-binding claim, peer exclusion, universal source contract or performance
claim follows. This review requires no replication of a load-bearing positive
observation because none is being accepted. Subsequent source explanation can
interpret the unexpected code explicitly but cannot rewrite this method/result.

No substantive reporting return. Preserve the failed criterion and unchanged
oracle; the single-run budget is exhausted. The next bounded source explanation
is proposed, unstarted, and grants no additional runtime authority.

## Return 24 — Mach-53 source explanation, claim 47

**Independent root review: accept scoped source explanation.** Root inspected
both the installed SDK and pinned public `kern_return.h` definitions, the
`MACH_TRAP` designated initializer and trap-96 table entry, and the filter,
debug-rejection and return branches in retained `bsd_arm64.c`. Return 53 is
KERN_DENIED. Trap 96 is not marked as returning a port, so a policy callback's
non-success value can survive unchanged while skipping handler copyout. A
separate debug-rejection branch can also return 53; this does not identify the
private running callback or prove the published source is the shipping kernel.

The explanation is consistent with claim 46's retained output and does not make
its exact-5 method pass. Accept no retroactive attribution, peer right exclusion
or complete guard closure. No substantive return.

For the architecture, source establishes a plausible explicit guard but does not
by itself qualify that private policy rule on the named OS. The smallest next
step is a separately identified exact-KERN_DENIED method with fresh controls and
replication if it succeeds, preserving the old mode and raw failure. Root permits
bounded code/build-only preparation of that method, not execution. No change to
target, profile, probe syscall or signing is needed; the new method changes only
the predeclared expected denial result and its versioned identification.

## Return 25 — fixed-53 primary, claim 48, replication pending

Root independently verified all 52 hashes and nine producing inputs of
`containment-mach96-denied-reviewed-01`, replayed the retained v5 classifier
under fixed-denied53-v1 and confirmed all three zero exits/no timeouts and
launch/positive controls. Both baselines return 5/zero output, while the amended
cell returns 53/unchanged sentinels. The new predeclared finite oracle passes;
the original exact-5 classifier still refuses attribution on these same outputs.

The primary is valid for the submitted finite comparison, with no substantive
reporting return. Acceptance of a load-bearing sampled rule effect remains
pending independent fresh-build replication. No complete capability exclusion,
valid-target peer acquisition, private callback identity or general HVF workload
claim is accepted. The separate replication-preparation review will state any
new run budget; none is granted here.

## Return 26 — fixed-53 independent replication readiness

**Translator review, 2026-09-20: ready for one separately budgeted fresh-build
replication; no execution performed by this review.** The reviewer did not author
the probe. Read claims 45/47/48 and the fixed-KERN_DENIED method, current retained
runner/probe/launch/policy/retention code, and the relevant public XNU handler,
filter-return branches and KERN_DENIED definition. The null target is rejected
before peer lookup in the inspected handler; the own PID is computed before
fixed-register assignment. The two volatile sentinel words are never treated as
acquired rights. The added policy differs only by commentary and the trap-96 deny.
This remains a copyout discriminator, not a valid-target acquisition test or a
proof identifying the private shipping callback.

Independently verified all **52** primary SHA256SUMS entries and byte equality of
all **nine** producing inputs across preparation, primary and current repository.
Both repository and retained `run.py --self-check` passed without invoking a
sandbox, compiler, signer or VM. Reclassification of the primary passes
`fixed-denied53-v1`; `legacy-exact5-v1` still rejects attribution. All three cells
have zero exit/no timeout and pass launch and positive-operation checks. Synthetic
checks distinguish exact return codes, missing copyout/control evidence, changed
sentinels, substituted unrestricted cells and missing resource/HVF positives.
Retained verification outputs:
`/private/tmp/natemu-mach96-replication-readiness-20260920T102247Z/`
(`checks.json`, both synthetic stdout/stderr pairs, SHA256SUMS).

The runner requires fresh external retention, snapshots inputs, recompiles both
binaries and records disassembly/signatures before execution. It stops if either
baseline fails; there is no repeated attempt or policy widening. Its existing
budget is three sequential one-vCPU/16-KiB workers, one peer, at most one worker
concurrent with that peer, 20 seconds per worker and 100 seconds for the execution
phase. Compiler/tool commands are separately bounded. Cleanup kills/reaps the
owned process groups; SETEXEC preserves supervision of the sanitized worker.
Inherited RLIMIT_CPU is currently unlimited and the VM-worker setter remains
omitted. The proposed fresh name
`containment-mach96-denied-replication-01` was absent at review.

No substantive readiness return. After root supplies the separate execution
budget, the unchanged invocation is `NATEMU_RAW_ROOT=/private/tmp/natemu-track03-raw
python3 experiments/track03/containment/run.py --name
containment-mach96-denied-replication-01 --mach96-denied-filter --execute`
(shown wrapped; one shell command). Fresh retention and subsequent independent
findings review remain required. This preparation does not supply runtime
authorization, count as replication, accept the whole containment guard or change
the failed legacy result.

## Return 27 — fixed-53 fresh independent replication result

**Translator replication, 2026-09-20: the finite oracle reproduced; submitted for
independent result review, no architecture self-acceptance.** Under root's separate
one-run authorization, executed the unchanged Return 26 command once. Exec session
`92036` reached terminal exit zero. Fresh build/sign/run artifacts are retained at
`/private/tmp/natemu-track03-raw/containment-mach96-denied-replication-01`;
manifest timestamps are 10:25:12–10:25:14 UTC. All 14 recorded context/build/
disassembly/signing commands exited zero. No retry or target/profile/method change
occurred.

All three sanitized workers exited zero without timeout. Both unrestricted and
base-restricted cells returned Mach 5 and zero output for both sentinel inputs.
The amended cell returned exactly 53 and preserved both sentinels. Every pre/post
word check, required launch check, own-resource/channel/HVF positive and self-task
facility check passed. The fixed classifier reports
`mach96_copyout_suppressed_after_added_rule`; the unchanged legacy-5 classifier
rejects attribution on these same cells.

Independently of the runner's own retention, this executor verified all **52**
retained hashes and exact identity of all **nine** producing inputs against the
primary. The recorded three-worker/one-peer, 16-KiB/one-vCPU, 20-second worker and
100-second execution-phase bounds were preserved. RLIMIT_CPU was unlimited
before invocation and recorded as such; no VM-worker CPU setter was added. The
runner completed owned process cleanup and reached terminal status. The single
replication budget is exhausted.

This reproduces the sampled null-target copyout suppression with the added rule
on the named M4/macOS host. It does not observe valid-target peer acquisition,
identify a private callback, prove every capability route excluded or establish
complete containment/HVF workload compatibility. The legacy failed method remains
unchanged. Root's independent review of these fresh artifacts and integration of
the source-conditioned implication remain next; no further execution is proposed
or authorized by this return.

## Return 28 — fixed-53 primary and independent replication accepted, claim 48

**Root independent findings acceptance, finite scope.** Translator independently
rebuilt and ran `containment-mach96-denied-replication-01` under the reviewed
budget. Root verified its 52 hashes and nine source identities against the
primary, replayed the retained fixed-53 classifier and checked actual per-cell
returns, sentinels, launch/resource/HVF positives and termination. The replica
completed 10:25:12–10:25:14 UTC with three zero exits and no timeout, reproducing
the primary's baseline 5/zero-output and amended 53/unchanged-output distinction.
No retry or input/profile/signing change occurred.

Accept the sampled Mach-96 rule effect on this M4/macOS 26.6.2 launch/profile:
the sole added rule suppresses the tested null-target handler-copyout path with
KERN_DENIED while preserving the finite positive operations. The policy's
call-number interface supplies a source-conditioned valid-target implication;
neither run acquires or tests a peer debug-control right. Do not infer a unique
private kernel branch, revoke already-held rights, close other acquisition routes,
qualify production backend grants, or claim complete containment. The earlier
exact-5 experiment remains negative/inconclusive under its own criterion.

No substantive return for this finite claim. Both explicit run budgets are
exhausted. This closes the sampled rule-binding uncertainty, not charter
question 12; remaining authority/grant and resource-lifetime obligations retain
their existing owners. No additional Mach-96 run is warranted by these results.

## Return 29 — explicit descriptor grants and held-token boundary, claims 49–50

**Root independent source/design acceptance, conditional scope.** Root inspected
both submissions and the retained pinned XNU spawn/exec descriptor handling,
`sys_fileport_makeport`, task-flavor conversion and
`task_identity_token_get_task_port`, with the SDK declarations. Explicit spawn
inheritance closes unspecified FDs under the stated recipe; fileport creation
starts from an already held fileglob. Token conversion is a separate authority
route with its own checks, including a self-CONTROL fast path. The observed PID
rule effects cannot establish that route's exclusion.

Accept the explicit stdio/disk/byte-pipe manifest as the conditional launch and
backend direction. Pipes carry bytes without an ancillary handle-transfer
operation; their broker still needs bounded semantic operations and resource
accounting. Disk object identity and mode must be checked before grant. Backend
I/O authority does not grant RAM commit or interrupt publication authority.
The current research launcher does not implement this complete production recipe.

Accept claim 50's remaining gap: absence of peer tokens and authority-providing
endpoints across actual process creation/exec has not been established. Null
bootstrap, empty registered ports and exception cleanup alone are insufficient.
No complete containment verdict or observed bypass follows. Trace that inheritance
boundary next; another identical PID/Mach-96 comparison would not close it.

No substantive return within this conditional scope. Editorially renamed the
transport title to avoid suggesting that a pipe itself conveys no authority.
No code, profile, native execution or budget changed.

## Return 30 — ordinary spawn IPC inheritance, root-authored claim 51

**Independent machine source review, September 20: accept the scoped narrowing.**
Inspected the retained kern_exec → kern_fork → task_create_internal → ipc_task_init
chain, checked both newly retained source hashes, and read the special-port
get/set and conditional exec-reset branches. No native observation is added.

The selected ordinary new-process spawn has no memory inheritance and does not
use the fork-only thread_dup path. ipc_task_init constructs a new IPC space and
own task ports rather than copying arbitrary parent names. A peer token merely
present in that name table is therefore not automatically inherited on this
path. The claim properly excludes SETEXEC variants and does not turn this into
a proof that startup libraries or explicit port actions cannot introduce rights.

The explicit registered/exception/host/bootstrap/task-access copies remain the
load-bearing qualification. Port actions are additional grants, not a mechanism
for declaring the entire namespace empty. The already-valid TASK_ACCESS_PORT
cannot simply be overwritten with null, and the public host-port setter has its
stated SIP-related restriction. MAC checks and task-flavor restrictions precede
user special-port retrieval; the copied-send branch is not unconditional public
access. Own task/thread capabilities remain distinct from peer capabilities.

The task-access discussion avoids the reverse overclaim: an authorization
endpoint used by kernel taskgated upcalls is not, by its name alone, a peer task
right or arbitrary-right broker. Its actual service contract and host-port flavor
still need classification. Likewise conditional credential/MAC-triggered reset
is not a blanket ordinary-exec purge.

No substantive return. Accept the source-based reduction to explicit special and
startup grants, not complete entry authority, private taskgated behavior, an
observed bypass, production launcher conformance or full peer isolation. The
proposed exact endpoint classification is the appropriate next bounded action.

## Return 31 — inherited service contract and host flavor, claim 52

**Root independent source/design acceptance, scoped.** Verified all eight retained
source identities and inspected the task-access MIG declarations, kernel task
access/signature callers, spawn credential installation, host copying/conversion,
ordinary-host IOKit acquisition and Mach initialization. The task-access messages
return scalar success/failure in this interface, not peer rights or a transferable
authorization ticket. Private receiver behavior is not established by that fact.
The signed-activation branch skips the inspected external signature lookup, not
every possible startup service.

Accept the non-root launch premise: the inspected credential path installs an
ordinary host port for nonzero effective UID. Merely inheriting a host port does
not itself attenuate it, and ordinary/privileged conversion remains distinct.
No all-host-operations safety inference follows. `host_get_io_main` explicitly
accepts ordinary host authority and returns the main-device send right without
bootstrap lookup. This is a concrete derived-service route left outside the
prior no-bootstrap/no-broker-transfer argument, not an observed policy bypass or
peer-memory capability.

Accept the conditional deny-or-explicitly-admit direction and the exact remaining
question: bind enforcement to that service operation and establish any required
startup/HVF dependency before claiming exclusion or adding a downstream grant.
The existing native profile has not been amended or tested for this route.
No substantive return for claim 52's source facts and scoped design reasoning;
complete containment remains open. Editorially point the acquisition citation to
`host_get_io_main` itself as well as the helper discussed in the text.

## Return 32 — ordinary-host IOKit MIG boundary, claim 53

**Root independent scoped source/design acceptance.** Verified the ten retained
primary identities; inspected SDK message naming, the installed airlock policy,
`ipc_kobject_server` pre-handler condition, filter-index registration and task
mask/callback installation. SDK 26.4 identifies message 205. The source path
rejects before the routine only when its mask/index/callback conditions reach a
non-success policy result. It does not imply that any chosen textual rule has
already installed that state. The shipped `host_get_io_master` spelling provides
a concrete candidate selector, not an empirical denial or a universal error code.

Accept the proposed narrow exclusion and the exact remaining uncertainty:
installed selector-to-message binding and preservation of required HVF startup/
map/run operations. Existing bootstrap restrictions and successful HVF fixtures
cannot answer this distinct kernel-MIG question. Public API signatures, Mach
startup and the export stub do not prove absence of private framework service
acquisition. Neither inherited rights nor user-space service messages are
revoked by this prospective kernel-object filter.

No substantive return for the source-backed boundary or conditional policy
choice. The next discriminating method needs a successful acquisition control,
fresh isolated worker authority and unchanged required HVF positives, followed
by separate pre-execution review and budget. No profile mutation, native run or
complete containment acceptance is implied by this return.

## Return 33 — independent primary MIG205 findings and replication preparation, claim 55

**Memory independent review; primary finite observation accepted, replication
prepared but not executed or authorized by this return.** Reviewed claim 54's
method and the separate primary execution budget against
`/private/tmp/natemu-track03-raw/containment-iomain-reviewed-01` (11:30:38–41 UTC,
2026-09-20). Independently verified all 53 retained checksums, all ten producing
inputs against both the current worktree and preparation 02, the three raw
worker outputs against the manifest, and the assessment by replaying the retained
runner's synthetic/self-check and classification functions. No native probe was
executed during this review.

The unrestricted and base-profile sanitized controls both acquired live send
rights: return 0, names 2819/7683, type 65536 (`MACH_PORT_TYPE_SEND`), followed by
successful acquired-right and host-right cleanup. The amended-profile cell
returned positive 53 with a null result, no acquired right to inspect or release,
and successful host-right cleanup. The installed SDK `mach/kern_return.h` defines
53 as `KERN_DENIED` (security-policy denial); it is not the negative MIG transport
error previously considered. Independently inspected the retained XNU
`ipc_kobject.c` pre-handler filter branch: the callback's non-success `kr` is
copied into the reply before skipping the kernel routine. This supports the
observed error classification, without claiming the public source forces 53 for
every policy rejection or is byte-identical to the installed kernel.

All three workers returned 0 without timeout or stderr; non-root identities,
zero inherited bootstrap/registered/exception counts, owned-file/channel
controls and required HVF create/map/protect/run/remap/destruction positives
passed. The restricted cells retained the expected open/signal/connect denials.
Prior peer CONTROL/READ baseline failures remain inconclusive and add no new
containment evidence. The recorded worker CPU-limit setter omission and bounded
three-worker, 20-second/cell, 100-second execution scope match the budget.

Accept the finite matched observation: the amended selector denied this fresh
ordinary-host IOKit acquisition while both acquisition controls and this fixture's
required HVF operations succeeded. This does not establish universal IOKit
exclusion, revoke existing rights, exercise a user client, select a complete
production sandbox, or replace independent replication. No substantive source,
control, retention or classification return remains for the primary record.

**Independent replication preparation:** all ten current inputs are byte-identical
to the primary retained `source/` and preparation 02, including the frozen
`experiments/track01/source/run.py` retention helper. The current runner SHA-256 is
`df5f2b6557d47522ca975872e3d85798caf99257a04b4c55161c73cb7555a2e8`;
retention helper is
`4f007c5121bc8e5cb5ec4ecf732e2a34542bd570c66daa893bb353f6249fe3dd`.
The proposed fresh destination `containment-iomain-replication-01` is absent.
The prepared method is the unchanged `--iomain-filter --execute` three-cell
fresh rebuild with the same 20-second/cell and 100-second total bounds, gates,
cleanup and stop conditions. Execution requires a separately recorded budget;
this review neither runs it nor permits retries or changes.

### Return 33 collection addendum — independent replica, acceptance pending

Memory executed the separately authorized fresh replication once after root's
explicit launcher-environment correction authorization. The initial missing
`NATEMU_RAW_ROOT` error occurred before allocation/build/native execution and is
retained separately as described in claim55. Session82251 is terminal, exit0.
`containment-iomain-replication-01` ran 11:37:23–26 UTC: both baselines acquired
live send rights; amended return53/null; all three worker exits0, no timeouts,
required controls qualified. Verified53 hashes and10 source identities against
the primary. This is the replicator's factual report, not self-acceptance of the
replication. Root independently reviews the retained artifacts next; the execution
budget is consumed and no further run is proposed here.

## Return 34 — root independent replication findings acceptance, claim 55

Root independently verified the sealed replica53 hashes, all producing source
identities against the primary, raw stdout/empty stderr and all three exact
`iomain_cell_ok` gates by replaying the retained classifier. Memory independently
rebuilt and ran `containment-iomain-replication-01` with the unchanged method.
Both baseline acquisitions returned0/live send rights and successful cleanup;
the amended cell returned53/null without a right. All workers exited0 without
timeout, and required launch/resource/HVF controls passed. The first launcher
invocation failed before allocation due to missing output-root environment; its
retained executor error and explicitly authorized correction are not a native
sample or an omitted contrary result.

Accept claim55's independently replicated finite installed-rule contrast on the
named M4/macOS/SDK context. It establishes exclusion of the tested fresh MIG205
acquisition with this fixture's HVF compatibility. Existing rights, other grant
routes, complete startup provenance, production workloads and overall containment
remain outside this observation. No method change, retry of an executed cell or
expanded target was used. No substantive return remains for this finite finding;
all native execution budgets are now exhausted.

## Return 35 — root independent acceptance of entropy choice, claim 56

Root verified five retained source identities and inspected the pinned QEMU
queue/completion path, Linuxv6.12 consumer, SDK26.4 getentropy contract and
Virtio1.3§5.4 normative requirements. The normative positive-short completion
permission supports a fixed256-byte staging limit without imposing an invented
request-size feature. Linux consumes the actual used length and posts a single
writable buffer. The source choice matches the queue8/VERSION1-only profile.

Accept the bounded native backend and shared admitted completion construction.
It has no guest-backed write before RAM admission, no host call while observers
are paused, no resampling on notification/coordinator retries, and no entropy
error-status field invented for a device that has none. Fatal native failure
uses the permitted contained-VM outcome. One retained job/result and fair owner
dispatch bound storage without importing an unneeded device quota timer.

Reset closes the incarnation, settles granted commits and retires calls/results/
interrupt offers before acknowledgement and reuse. A slow native call cannot
justify freeing its buffer, blocking the control owner, or falsely acknowledging
a drained reset. Payload/ring aliases are tracked before observer release.
These rules compose with the accepted block/RAM window rather than duplicating
an asynchronous guest-memory authority path.

No substantive return. The entropy subchoice is accepted under the stated shared
transport/coherence/IRQ premises. Production syscall-policy conformance remains
required; the SDK's sandbox statement is not observed acceptance of the final
custom profile. Stock boot, random-quality measurement, actual device conformance
and throughput are not established. No additional mock/probe is needed to settle
this source-based interface and backend choice.


## Return 36 — network profile and packet-broker interface

**Reviewer:** root, independent of machine claim57 authorship, September20.
**Verdict:** accept the guest queue/ownership subchoice and conditional native
backend direction; production broker authority remains unresolved.

Inspected Virtio1.3 §§5.1.6.2–4, Linuxv6.12 header-size/MAC/MTU/small-buffer
paths, pinned QEMU vmnet start/read/write/stop and packet staging, and SDK26.4
vmnet declarations. Verified the four retained source identities. The modern
12-byte header, RX num_buffers=1 without merge and zero TX num_buffers agree
with the specification; Linux's ordinary receive allocation includes VLAN
headroom. The one-packet SDK result/count interpretation and read buffer bound
match the declared API. QEMU explicitly disables generated backend MAC
allocation; preserving that choice avoids conflicting source-MAC assumptions.

The staged TX/RX buffers replace guest-backed IOV lifetime across asynchronous
work. Admitted payload/ring publication, source/PTE aliases, typed notification
and reset reference-drain compose with the accepted memory contract. Network
loss does not require retrying ambiguous sends. Native read/write/stop callbacks
remain outside the device owner and RAM grant. The stronger reset rule concerns
already buffered old-incarnation work; it does not promise to recall network
packets or identify a late remote response as belonging to a past guest request.

A review return corrected an inferred blanket host-network prohibition. The
charter requires ordinary networking and excludes unauthorized process/state
access; it does not require an air gap. The revision explicitly grants shared
NAT's documented host-service reachability while retaining separate broker and
worker rights. The isolation key is correctly limited to its documented vmnet
interface scope. No guest-controlled interface selection, rights transfer or
RAM grant is added. Production broker entitlement/minimum service grants and
its actual failure boundary remain conditional, not established by the previous
execution-worker sandbox probes. No broader containment acceptance follows.

No substantive return remains for this scoped interface. The next source task
must settle that real backend authority premise; a queue mock or successful
packet alone would not do so. No performance or complete device-architecture
claim is accepted.


## Return 37 — native packet-broker launch recipe and alternative

**Reviewer:** root, independent of machine's claim58, September20.
**Verdict:** accept the conditional recipe and scoped platform findings.

Independently checked Apple's restricted vm.networking entitlement description,
the retained authorized-profile/app-like-bundle guidance, installed application.sb
NetworkSharing rule and the eight retained source identities. The signing article
supports embedding an authorizing profile; it does not authorize this account or
require copying its sample's root daemon deployment. The separate new vmnet key's
sparse metadata supplies no interchangeable permission contract. The investigation
correctly avoids assuming that the actual account lacks authorization.

A distinct broker bootstrap/service context is necessary for the proposed named
lookup; the execution worker's NULL-bootstrap isolation is not silently undone.
The single shipped policy rule is correctly treated as a lower bound, not a
complete restrictive framework launch/callback/stop policy or proof of the
returned capability set. Packet-only framing and no guest RAM authority retain
the selected interface. Actual broker privilege/resource containment remains
conditional until the private client requirements are established.

Checked QEMU's slirp dependency pin and the pinned ICMP path: it tries datagram
ICMP, has Darwin layout handling and a raw-socket fallback on selected errors;
UDP echo fallback does not establish ordinary ICMP equivalence. Consequently
libslirp is a concrete alternative worth assessing at the same broker boundary,
not a proven protocol-equivalent replacement or an automatically TCP-only design.
No substantive return remains for the stated scope. Neither native deployment,
network protocol conformance nor complete containment is accepted by this
source pass. A new packet mock would not resolve the identified authority gap.


## Return 38 — userspace network protocol and authority boundary

**Reviewer:** root, independent of machine's claim59, September20.
**Verdict:** accept after the DNS source-interpretation correction.

Inspected the pinned ICMPv6 switch, Darwin resolver loop, public configuration,
flow allocations and retained source identities. External echo reaches an
explicit unimplemented branch; local echo and neighbor discovery do not supply
that path. This rejects unchanged reuse for the stated complete network
contract, not all libslirp-based adaptations. No runtime is needed to demonstrate
an absent branch implementation.

The initial DNS description followed the comment's intended first-family match.
Review found that the copy actually occurs under found==1 outside the family
condition: a subsequent other-family record can overwrite the chosen address.
The author corrected the claim and names mixed-family handling as an adaptation
obligation. This is source reasoning, not an observed resolver failure.

The stack's dynamic flow/socket/heap allocations are correctly distinguished
from two bounded channel slots; MTU/MRU and descriptor limits do not prove an
aggregate memory bound. Callback serialization/retirement and actual resolver/
socket rights remain separate requirements. The IPv4 Darwin path is not falsely
rejected on a generic user-network warning, nor is UDP echo represented as ICMP
equivalence. No substantive return remains in the stated scope. Native vmnet
and a completed userspace adapter retain their separate open applicability
premises; neither is selected through a silent reduction in network behavior.


## Return 39 — COM1 serial contract

**Reviewer:** memory, independent of machine's claim60 author, September20.
**Verdict:** accept the scoped serial register/IRQ/backend/lifecycle choice after
one API-sign correction; no runtime/boot or complete machine acceptance.

Verified all eight primary hashes in
`/private/tmp/natemu-track03-serial-01/sources.json`, then read the pinned QEMU
ISA registration/AML, UART register effects, transmit/watch retry, receive-count,
timer and reset paths, and Linux v6.12 earlycon, PNP and8250 IRQ/port sources.
COM1 ports0x3f8–0x3ff/IRQ4, PNP0501 and the Linux byte-I/O earlycon route match.
The QEMU AML's level/low/shared declaration differs from the selected edge/high
ISA route; claim60 explicitly adapts it rather than falsely claiming inherited
agreement. The Linux ISA handler's drain requirement supports preserving actual
line transitions rather than repeatedly injecting a held-high condition.

The callbacks assert one-byte register accesses, while serial_io_ops declares
one-byte implementation width and little-endian decomposition. Captive wider
port requests therefore need the selected bus adapter and whole-request effect
record; the raw UART callback cannot accept arbitrary x86 width. RBR/IIR/LSR/MSR
side effects and string-I/O progress are correctly separated from transport
retries and guest INS destination writes.

The TX adapter binds a real source hazard: zero/EAGAIN preserves the shift byte
only with a successful watch and below the finite retry limit. Reserving space
before a single owner callback prevents spurious writable events from exhausting
that retry path. Bounded connected backpressure and explicit detached-sink loss
are distinct, with other owner/control progress retained. The UART has no guest
RAM alias; channel cancellation/incarnation lifetime composes with claims49 and
the memory-owner rules. RX trigger-threshold zero is respected rather than
mistaken for remaining FIFO capacity. FIFO timeout, EOF and loopback remain
model-owned. Cold reconstruction plus timer/watch retirement avoids assuming
serial_reset clears fcr; shutdown's bounded console drain is not disk durability.

**R39-1 corrected and closed:** the first submission said return ENOTSUP for
unsupported modem ioctls. The actual serial_update_msl path compares exactly
`-ENOTSUP` before using returned flags. Requested explicit negative return;
author amended the text and I rechecked it. No producer or author-contract fix
was written by this reviewer. No other substantive return remains.

Acceptance selects this bounded adapter contract, not unmodified QEMU host-loop
integration or proven lossless external logging. Target kernel configuration,
firmware publication and existing controller/clock/memory premises stay explicit.
No new execution, UART implementation or generic mock is required by this review.


## Return 40 — timer-arm authority and delayed-expiry gap

**Reviewer:** root, independent of machine claim61 authorship, September20.
**Verdict:** accept the scoped programming/cancellation interface and the identified
source gaps; question9 remains open.

Verified all seven retained source identities and inspected the QEMU LAPIC count/
expiry arithmetic, PIT callback and transition paths, RTC periodic rescheduling/
PF latch, PM overflow/status-clear functions, and Intel253668-092US §13.5.4.
The single-owner arm identity and due-before-register-operation rule give a
concrete linearization choice. Stale wake rejection is correctly distinguished
from clearing already committed architectural pending state and from callback
object lifetime. Pause uses the accepted shared epoch; per-vCPU TSC writes leave
the other clocks untouched. No frequency is inferred from callback latency.

The source discrepancies are real within the pinned model: LAPIC arithmetic is
based on virtual nanoseconds and periodic N+1, while the selected machine reports
a24MHz crystal; PIT and RTC periodic callbacks advance from their old scheduled
times. A delayed owner can therefore retain many overdue callbacks. Merely
sampling PIT's final level can miss an intervening rising edge. Selecting RTC
DISCARD changes reinjection behavior but does not itself fix that rescheduling.
These source findings are not observed runtime failures or a rejection of model
reuse. They identify the precise adaptations needed before accepting question9.

The next argument must cover event occurrence as well as final counter/line
state, serialized with register/IACK/EOI effects, and the LAPIC divider/mode
phase convention. Constant-bounded pending state cannot be assumed to justify
throwing away a distinguishable event. No substantive return remains for61’s
stated scope; no runtime/mock or full timer acceptance follows from this review.


## Return 41 — delayed timer folding must preserve TMR as well as IRR

**Reviewer:** root, independent of machine claim62 authorship, September20.
**Initial disposition:** return one consequential receiver-state gap; question9
remains open.

Inspected Intel8254 231164-005 mode/gate/load/count contracts, Intel253668-092US
§§13.5.4/13.8.4, and pinned PIC/IOAPIC/APIC source paths. Selecting exact PIT
source phases rather than reproducing QEMU’s nanosecond quantization is a valid
explicit adaptation direction. Finite pending state can replace callback replay
only when all relevant receiver state and interleavings are preserved.

The current argument accounts for IRR/ISR and IOAPIC remote-IRR but omits local
TMR. Intel §13.8.4 says acceptance clears TMR for edge and sets it for level;
pinned apic_set_irq likewise updates TMR on each offer. Two timer sources may
use one fixed vector with different trigger modes. Applying independently folded
source summaries in arbitrary order can leave the wrong TMR even without any
IRR consumption, changing a later EOI’s broadcast behavior. A non-timer offer
during the folded interval raises the same ordering issue. Saturated IRR alone
is not a complete receiver-state equivalence proof.

Smallest remedy: preserve the order of consequential accepted offers, accounting
for IOAPIC remote-IRR suppression, and establish cuts for other offers that can
change that receiver state. A bounded first/last-event summary may suffice, but
its reduction needs an explicit argument. Do not forbid a legal shared-vector
configuration or defer this specific folding gap wholesale to question8. No
new hardware experiment is needed to establish this source counterexample.

**Recheck and final verdict:** accept amended62 and the scoped question9
clock/timer contract composed with29/61 and INV03-01 claim13. Re-read the
complete source-phase construction, verified the retained Intel8254 PDF identity,
and checked the amended reduction against Intel §13.8.4 and apic_set_irq/EOI.
The amended source summaries preserve first/last accepted edge occurrences and
the first eligible level offer under remote-IRR gating. Cross-source time order
and a declared simultaneous tie rule determine final TMR. Non-timer/IPI offers,
TMR queries and receiver consumption now establish cuts. The edge/level/edge
counterexample therefore preserves IRR, TMR and remote-IRR separately. The bound
depends on configured routes rather than elapsed periods. R41-1 is closed.

8254 mode/gate/reload state and exact rational phases give finite prefixes plus
periodic quotient/remainder advances; the design explicitly adapts upstream
quantization rather than treating it as required hardware behavior. RTC/PM
flags remain latches with clear/programming cuts. The LAPIC choice preserves
countdown/reload/zero-stop and mode-write-not-start. Restarting fractional divider
phase is labeled a deterministic virtual-model choice where the cited Intel
contract does not prescribe sub-tick retention, not measured Haswell behavior.

No substantive return remains for this clock contract. Its receiver cuts and
source-model adaptations are mandatory; a future optimization must preserve
them. Question8 still owns complete enabled routing/event semantics, and a
conflicting choice reopens composition. Timer implementation, precise native
latency, PIT-helper conformance and synchronization cost remain unestablished;
no more callback replay experiment is required merely to accept the design.


## Return 42 — claim63 routing and event composition

**Reviewer:** root, independent of machine's authorship. **Verdict:** accept
the scoped routing, ExtINT and event/wait composition; question8 remains open
on the explicitly identified core-event retention/reset contract.

Read claim63 and rechecked Intel Vol3A092 §§7.7–7.9 and13.6.2.4 against the
retained primary text, plus Intel82093AA's redirection delivery table and edge
acceptance description. Minimum-TPR selection and a stable equal-priority tie
are an explicit virtual choice supported by the chipset arbitration description;
they are not QEMU's first-bitmap shortcut or a claim of physical fairness.
Route/TPR changes cut timer folding, while PPR/IF remain delivery eligibility.

The82093AA explicitly requires edge IOAPIC ExtINT. Its busy delivery status
prevents recognizing another edge until destination acceptance, so one owned
request with a typed INTA completion does not require an unbounded edge queue.
This is distinct from LINT ExtINT and from level remote-IRR/EOI bookkeeping.
Deferred IACK preserves once-only controller effects across delivery faults.

The SDM explicitly establishes NMI blocking before delivery and unblocking even
on faulting IRET; native repair cannot substitute for either transition.
Table7-2 remains a between-instruction priority table, not a universal operand
fault ordering rule. Durable pending state and reserved control capacity make
the wait argument independent of guest IF and compiler/RAM progress.

No complete NMI multiplicity, INIT/SIPI reset/precedence or SMI reception rule
is established by this acceptance. Those precise gaps remain with question8;
SS/debug fusion and nested-delivery semantics keep their existing owner. No
runtime, performance or whole-architecture acceptance follows.


## Return 44 — claim64 bounded NMI and INIT/SIPI ownership

**Reviewer:** root, independent of machine's authorship. **Verdict:** accept
the scoped NMI retention and INIT/SIPI state/ownership construction. Question8
remains open on SMI reception and its question10 startup boundary.

Read the complete claim and rechecked retained Linux v6.12 native nmi.c and
KVM process_nmi, Intel3A092 Table12-1 including notes2/5/6, and Intel3C081 SMM
entry/exit discussion. Native Linux explicitly describes a binary hardware
NMI latch; KVM separately distinguishes one blocked pending request from its
two-request unblocked scheduling accommodation. The selected pending bit plus
active delivery record is a bounded compatibility construction with explicit
reception acknowledgement, not an inference from QEMU's pending bit alone.
No sender waits for a target IRET, and the architectural latch is distinct from
transport deduplication.

The INIT table correctly retains vector/x87/XCR0 and time state while resetting
control/segment/EFER state; CD/NW preservation and the FINIT-versus-INIT footnote
are material. Derived translations cannot survive merely because guest memory
and cache contents do. The ordered transition commits preserve earlier external
effects and reject stale continuations. They do not equate a prepared startup32
entry with hardware's PE/PG-clear INIT state.

SMI reception genuinely requires a separate decision: its state-save/SMM/RSM
semantics do not follow from disabling a firmware SMI trigger. This review
accepts neither a silent no-op nor an unrequested general real-mode/SMM engine.
The precise remaining issue is the exposed virtual machine's supported boundary
and compatibility consequence. No whole event-contract, runtime or physical
Haswell equivalence is accepted here.

## Return 43 — claim65 Darwin unprivileged ICMPv6 mechanism

**Reviewer:** translator, independent of root's authorship. **Verdict:** accept
claim65's scoped source mechanism and conditional adapter obligations; no complete
network backend or platform-conformance acceptance. No substantive return.

Read claims59/65 and all four retained Apple `xnu-12377.1.9` files in
`/private/tmp/natemu-track03-icmp6-source-01`. Independently checked their lengths
against sources.json and inspected the actual branches. This review used retained
primary copies, not a fresh independent download; their release identity is not
asserted to match the running kernel byte for byte.

- `icmp6.4` explicitly documents unprivileged AF_INET6/SOCK_DGRAM/IPPROTO_ICMPV6.
  `in6_proto.c:235` connects that socket type to `icmp6_dgram_usrreqs` and its
  restricted option handler. `icmp6.c:3300` omits `rip6_attach`'s superuser gate;
  send at3233 permits echo request/code0 and specified NI queries, otherwise
  returns EPERM. This supports availability without root, not sandbox permission,
  unrestricted message transmission or an entitlement grant.
- The shared `rip6_output` constructs the host IPv6 header, selects the host
  source and overwrites the checksum field before `in6_cksum` (`raw_ip6.c:554`,
  666,702–727). Guest addresses/checksum cannot be forwarded as though they were
  the host packet's authority. No automatic guest echo-identity remapping is
  established by this code.
- The relevant receive chain is `icmp6_input` to **`icmp6_rip6_input`**, not merely
  the similarly shaped generic `rip6_input`. In `icmp6.c:2115–2222` it matches
  protocol, scoped local/remote addresses and ICMP type filters, applies receive
  restrictions, saves ancillary context and strips the IPv6/intermediate headers.
  It has no echo identifier/sequence match. `icmp6_dgram_ctloutput` admits packet-
  info/hop-limit options. An adapter must still identify its outstanding request
  and validate replies/errors; receiving an ICMP packet is not proof it belongs
  to a particular guest or request incarnation.
- `icmp6_input:600–649` handles destination-unreachable, packet-too-big and
  time-exceeded before error notification and socket delivery. In particular,
  this source drops PTB values below IPV6_MMTU. The manual's receive-error support
  is therefore useful mechanism evidence, not an exhaustive guest error/MTU
  translation contract or guaranteed receipt of every wire message. Claim65
  correctly keeps that translation and host send-error mapping unresolved.

Rechecked retained libslirp `ip6_icmp.c:415–423`: the external echo branch still
logs its unsupported condition and frees the packet. Darwin's available socket
mechanism does not erase that implementation gap. The proposed bounded request
admission, private copies, single owner and incarnation/drain rules are adapter
requirements, not observations that an unchanged component already satisfies.
The explicit outstanding sandbox/resolver authority, DNS family/scope correction,
reply/error/MTU mapping and aggregate allocation bound preserve question11's full
scope. No raw/root fallback, UDP-echo substitution or arbitrary packet passthrough
was accepted. No socket creation, native experiment, code change or runtime
occurred during this review.


## Return 45 — claim67 adapted broker interfaces and resource admission

**Reviewer:** root, independent of translator's authorship. **Verdict:** accept
the scoped reply/error, DNS correction and allocation-admission contracts.
Complete backend selection remains conditional on the named platform/protocol
binding, not on a new generic networking survey.

Read67 and rechecked retained libslirp icmp6_forward_error, resolver selection,
sotranslate_out6 and UDP FIONREAD/growth path. The error helper really hardcodes
interface MTU and lacks Parameter Problem initialization; passing a host PTB
through it unchanged would produce a wrong guest MTU. The typed replacement
retains actual MTU/pointer provenance and original guest quotes. Scoped-source
matching and router-origin errors are distinguished correctly from echo replies.
No complete extension/fragment or EMSGSIZE mapping is inferred.

The DNS correction binds copy to a matching family and retains IPv6 scope,
which the existing output translator can carry. It does not promise split-DNS
equivalence. Installed sandbox service names remain discovery leads, not an
authorized broad allowlist or proof of the runtime provider's exact needs.

Preallocation byte/object admission addresses heap growth that a flow or file
descriptor limit cannot bound. Gateway coverage and numerical tuning are
implementation-conformance/planning duties under the selected invariant, as
the author's final clarification states. Kernel socket/provider resources need
the separately stated OS limits; they are not charged fictitiously to library
heap B. Reserved control capacity and pinned retiring objects remain explicit.

No substantive return remains within67's scope. An actual usable MTU observation,
resolver service binding and enforceable platform limits remain specific source/
platform questions. Existing launch/MIG205 evidence is retained; no new socket
probe, runtime authorization, generator preference or complete containment
conclusion follows.


## Return 46 — claim66 fixed SMI firmware and event-contract composition

**Reviewer:** root, independent of machine's authorship. **Verdict:** accept66's
protected immediate-RSM firmware choice and the scoped question8 routing,
reception, pending-state, wake and coordination contract composed with23/27–28/
62–64. Precise instruction/event delivery and concrete boot/startup policy remain
the named question5/10 interfaces, not inferred implementations.

Read66 in full and independently checked Intel3C081 §§32.2–32.5,32.8,32.10,32.12
and Intel3A092 §7.9. The primary text supports one latched SMI during SMM,
retained preexisting NMI blocking, pending SMI before startup processing, the
permitted STI/SS inhibition, default auto-HALT restart and zero I/O restart.
The between-instruction priority rule supports retiring a lower debug candidate;
it does not erase committed DR6 or order faults inside another instruction.

This is a substantive platform choice, not a renamed no-op SMI. Immutability and
closed/locked SMRAM make a permanent semantic service a valid realization of
the selected one-instruction firmware. A guest-writable handler/save area or
reopenable bridge path would invalidate the shortcut. Unique reserved banks,
initialized SMBASE and the lock must be part of the boot manifest. The state
save/return preserves live banks and event/time/device owners rather than
rolling back a whole-vCPU snapshot. Store completion uses existing authority;
no nested RAM-grant wait is introduced.

The ordinary Linux platform promises neither OEM firmware services nor general
guest replacement of SMM code. This choice therefore preserves legal SMI
reception within the declared virtual platform without silently broadening
real-mode support. RSM outside SMM remains an ordinary #UD condition.

Combined with63's coherent routing/ExtINT/cuts and64's bounded NMI/INIT/SIPI
ownership, the previous nonfixed-event reception gap now has a concrete bounded
realization. Each pending class has an owner; receipt does not depend on guest
handler progress; native wakes do not stand in for architectural delivery.
Question10 still must define the admitted startup-assist and optional restart
policy, and question5 must close full delivery/partial-effect semantics. A
conflict in either reopens this composition. Those obligations are visible
architecture work; accepting8 does not claim stock boot, native conformance,
minimum latency or complete Track03. No new experiment is needed merely to
establish the immutable-firmware realization.


## Return 47 — claim68 Darwin MTU and typed DNS provider

**Reviewer:** root, independent of translator's authorship. **Verdict:** accept
the scoped source findings and conditional API binding. This does not close
network authority or accept complete backend selection.

Read68 and independently inspected retained ip6_output.c's IPV6_PATHMTU getter,
libresolv res_init conversion/res_getservers, configd dnsinfo_copy and the
libSystemConfiguration client service/loop. The getter requires a connected
socket, returns a zero-initialized ip6_mtuinfo with only MTU populated, and
explicitly omits some route-option considerations. The separate bounded query
construction avoids confusing connection-filtered reception with receipt of
router-origin errors. A route estimate is not proof of an earlier EMSGSIZE cause.

The resolver source really loses IPv6 scope through text conversion and parses
only its chosen default resolver into BIND state. Correcting libslirp's family
copy cannot restore information already discarded. The direct typed snapshot
provider retains the needed fields; its exact DNSConfiguration service is a
concrete authority requirement. Its synchronous retry loop requires independent
supervision; named lookup and a library call alone do not bound owner latency.
No broader notify/file/service grant is inferred.

The received-ICMP fanout is a material boundary, correctly retained as unresolved.
Application matching protects the intended adapter path but cannot constrain
a compromised process holding the socket. Ordinary authorized networking and
host-service reachability do not alone prove authorization to observe unrelated
host/peer ICMP quotations. A narrower trusted endpoint or another supported
backend boundary must address this before complete containment/network
acceptance. No broad packet-capture permission is created by this review.

No substantive return within68's stated scope. Full protocol correspondence,
selected DNS scope and actual broker authority remain concrete decisions;
no socket run, implementation, platform-wide policy proof or performance
claim follows.


## Return 48 — claim69 boot manifest and lifecycle

**Reviewer:** root, independent of machine's authorship. **Initial return:**
the proposed CF9 byte reset register overlaps the PCI configuration-address
region while claiming a nonoverlapping selected decode map. Bind explicit
width-specific arbitration or choose a separate virtual GAS port; do not leave
a board-composition ambiguity.

**Recheck and final verdict:** accept amended69 as the scoped question10
loader/map/topology/startup/lifecycle contract. The author moved reset to B040,
outside PMBASE B000–B03F, GPE AFE0–AFE3 and PCI CF8–CFF, with the unused SMBus
decode disabled. FADT and its owned reset register now agree. The return is closed.

Independently checked retained Linux64-bit boot protocol, runtime_start/init_size
calculation, ACPI reset consumer and mailbox consumer, plus the actual audited
Ubuntu config. Virtio/MMIO/block, ext4/JBD2/mbcache, partition parsers and devtmpfs
mount are built in for that target, supporting the declared no-external-initrd
installed-root admission. Debian's actual generated initramfs must satisfy its
separate declared dependency closure; the installer archive is not mislabeled.
Missing future root-image inputs are normal loader-admission obligations, not
proof of an existing boot or a reason to reduce the product to an installer.

The fixed map keeps native private state outside guest physical ownership and
reserves the protected SMM banks and live AP/table records. The revised loader
explicitly equates the aligned relocatable K with runtime_start, checks both
loaded/runtime extents, and uses checked inclusive/exclusive initrd limits.
Topology and interrupt/device identities derive from one manifest. Writable
mailbox/table behavior and retained bootstrap references are explicit.

ACPI reboot is an added virtual register/table contract, not inherited PIIX
behavior. Admitting that write stops guest execution before BIOS fallback;
reconstruction creates a new lifetime without rolling back disk effects. S5,
truthful flush and failure reporting retain the accepted storage contract.
Unsupported optional lifecycles are declared; native management still must
remain responsive. UEFI/Windows implications are correctly separated from
this direct-Linux loader and do not imply feasibility or impossibility.

No substantive return remains for this architecture contract. Actual Ubuntu
image bytes, per-launch root/initramfs inputs, emitted tables and stock boot
need implementation validation. CPU/precise-delivery/device/containment contracts
remain named dependencies. No runtime, combined architecture acceptance or
implementation authorization follows from accepting question10.
## Return 50 — claim71 IPv6 packet-layer gap

**Reviewer:** root, independent of translator's authorship. **Verdict:** accept
the scoped source counterexample and required adaptation boundary.

Read retained pinned ip6_input/ip6_output, udp6 and socket receive code, with
if_output/if_encap. The input dispatcher has no fragment/extension path; UDP
assumes a fixed base-header offset. The output queues one datagram without
fragmentation. Recomputed the example:2008 transport bytes split1448/560 give
1496/608 IPv6 packets and offsets0/181; the reverse unfragmented packet is2048.
These are valid sizes for endpoints supporting the stated reassembly length.

Independently checked RFC8200 §§4.5,5,8.1: atomic fragments are independent,
reassembly has finite timeout/overlap rules, and upper-layer checksum length and
destination differ from blindly using the base-header fields. The1500-byte
minimum reassembly requirement is not a universal prohibition on larger packets.
Individual-link admission and completed-datagram admission must be separate.
The finding correctly preserves ordinary networking rather than accepting a
small-datagram-only substitute. Its remedy remains an architectural obligation,
not proof that a fragment adapter already exists. Claim72 owns the finite
construction. No networking execution or complete question11 acceptance follows.
## Return 49 — claim70 per-VM containment and teardown contract

**Reviewer:** root, independent of translator. **Verdict:** accept the corrected
scoped question12 architecture under its named OS/private-policy premises and
explicit trusted boundary; not an accepted production sandbox implementation.

The initial text conflated the ICMP endpoint's grant with DNSConfiguration and
described a no-socket worker while also placing libslirp in the contained domain.
The author corrected this to four explicit roles. Root rechecked: execution/
device worker has no network acquisition; fallible libslirp owns authorized
TCP/UDP sockets; trusted ICMP endpoint alone receives ICMP fanout; the separate
DNS provider alone obtains the named snapshot service. Neither raw receive
buffers nor native rights are released to the fallible stack. Both returns close.

Read retained XNU syscall table and mach_host definitions against49–55's accepted
capability provenance and finite policy evidence. The additional process-control
surfaces are real and are not required worker grants. Host-port factories cannot
be equated with mere timing queries. The construction relies on named private
MAC/syscall/MIG enforcement plus role-specific capability manifests, not failed
peer-acquisition baselines or a claim that MIG205 covers every service.

The ICMP matcher is explicitly trusted and isolated from the larger fallible
stack. Its compromise is not claimed contained by its own filtering; ordinary
crash/hang/allocation failure remains per-VM. This is a real TCB/cost addition,
not new packet-capture authorization for guest code. Trusted OS/supervisor and
minimal authority mediation are a declared boundary consistent with isolation
and crash containment, rather than a promise to contain failure of all trusted
code. Broader hostile host-resource denial-of-service quotas are not established.

Admission accounts for replacement peaks and retiring references, reserves
control progress, distinguishes kernel resources, and allows contained failure.
The teardown contract composes with accepted storage durability and69's new
incarnation: late work cannot target reused resources, and failure cannot invent
flush or cancellation success. Required grant/policy/HVF conformance remains
implementation acceptance; unavailable enforcement rejects admission. No further
unbounded denial campaign is a condition of accepting this design contract.
Question11's protocol choice and combined-architecture review remain separate.
## Return 51 — claim72 finite IPv6 normalizer

**Reviewer:** root, independent of translator. **Verdict:** accept the scoped
packet-layer construction and its containment/resource composition.

Rechecked71's pinned ingress/egress seams and the additional IPv4 ip_reass and
output fragment paths. Read RFC8200 and the cited primary fragment, ECN, ND and
ICMP error rules. The new entry explicitly bypasses the old link-MTU dispatcher
after reassembly, retains real transport checksum inputs and original quotation
coordinates, and preserves fragmentation provenance for local ND rejection.
Its bounded contexts distinguish atomic fragments, overlap, missing headers,
timeout and malformed lengths; packet-length progress and charged retained state
avoid an unbounded native recursion. The selected ECN mixture policy is permitted
by the cited reassembly rules. No numerical link MTU is mistaken for an
application datagram limit.

The egress proxy originates packets, so source fragmentation is the relevant
contract. Root checked the1448-byte chunk and46-fragment maximum arithmetic.
Checksum precedes fragmentation; publication owns all live buffers and IDs.
Loss after partial publication is ordinary network loss, not rollback or successful
delivery. ICMP error eligibility/rate limits and original data ownership survive
normalization. This parser remains in the fallible network process and adds no
authority to70's minimal trusted ICMP endpoint.

The decision adds an explicit implementation component rather than claiming
fragmentation already exists in libslirp. IPv4 inherited paths remain distinct;
IPsec/source routing/jumbograms are not smuggled into an ordinary socket-proxy
contract. Actual parser/transport/platform conformance remains planning work.
Question11 still needs the combined device/backend decision, not another generic
protocol survey. No runtime or measured performance acceptance follows.
## Return 52 — claim73 required-device composition

**Reviewer:** root, independent of translator. **Verdict:** accept the scoped
question11 device/backend contract, conditional on its named platform premises.
Claim72's prerequisite is satisfied by Return51.

Compared73 against accepted block26–29, entropy56, network57/67–72, serial60,
boot69, containment70 and memory/interrupt ownership. Device addresses, IRQs,
features and launch discovery agree. The selected network backend is explicitly
adapted libslirp; vmnet remains a conditional alternative, not an unproved
fallback. Required large UDP uses separately owned reassembly and ordinary link
fragments, preserving the existing2048-byte device slots and no-offload profile.

The common snapshot/host-work/admitted-completion sequence preserves RAM and
source/PTE ownership without holding exclusion across host work. Status/ring
publication precedes notification; notification suppression does not suppress
completion. The role manifests exclude network access to disk/RAM and retain the
trusted ICMP/DNS boundary. Reset and shutdown distinguish buffer release from
remote delivery or persistence, retain real partial effects, and prohibit stale
callbacks into a new generation. This composes with truthful block flush and
the cold-reconstruction lifecycle without adding an atomic-I/O fiction.

No new architecture-changing device contradiction was identified in this
composition. Actual policy admission, emitted tables/adapters, packet/queue/reset
conformance and costs remain implementation/measurement obligations. Acceptance
does not certify unchanged upstream code or a completed device implementation;
questions2/5 and combined-architecture breadth remain open.

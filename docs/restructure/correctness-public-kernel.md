# Additional public CPU/RAM tests — November 7, 2026

Status: **complete: 400 initial boots plus 42 follow-ups**. This is a bounded correctness campaign on
`correctness` commit `8a2a6c4`, not a claim of exhaustive CPU coverage.
Artifacts: `/Users/liaozping/orca/natemu-raw/public-cpu-ram-20261107-01/`.
The [source survey](../research/notes/cpu-ram-test-suites.md) explains selection.

**No new natemu CPU/RAM defect was confirmed.** Every one of the 442 attempts has
retained output and a reviewed disposition. No outer guest/host caps, missing
results, foreign-VM overlaps or input/output integrity discrepancies occurred.
The ptrace test's internal timeout remains a failure, not a pass.

| Executions | Completed | Clean passes | Qualified passes | Failed attempts |
| --- | ---: | ---: | ---: | ---: |
| Initial userspace selections | 284 | 271 | 4 | 9 |
| Initial kernel selections | 116 | 112 | 4 | 0 |
| Fixture, diagnostic and repetition follow-ups | 26 | 10 | 8 | 8 |
| IRQ locking and extended RCU/SRCU follow-ups | 16 | 16 | 0 | 0 |
| **Total** | **442** | **409** | **16** | **17** |

These are boot counts, not individual assertions. Qualified passes are twelve
COW runs with explicit skips and four vmalloc runs with the expected loader
exit. Failed attempts remain recorded after retries. Their causes are listed
below; none is silently removed from the totals.

## Execution contract

Run the low/moderate integration candidates unchanged: Linux 6.12.107 rseq,
futex, MM, membarrier and ptrace selections; selected LTP RAM/futex tests;
QEMU x86-64 instruction regressions; kernel memcpy/usercopy KUnit, atomic64,
bitmap, vmalloc, locking/RCU torture, and an LKMM corpus through klitmus7.
Cyberus and kvm-unit-tests boot integration are outside this batch.

Each selected command runs on natemu and QEMU TCG at its eligible CPU counts.
Use the stock Debian 6.12.107 kernel for userspace tests. Kernel tests use a
separately retained 6.12.107 build with the selected test frameworks enabled;
its exact configuration is part of the evidence. Public sources are not patched.
The recorded native x86 address `192.168.12.11` and the owner's corrected
`192.168.12.10` both failed before SSH authentication (No route to host);
the latter also timed out from the Linux container. A local
Debian container cross-builds the tests; it is not a native x86 oracle.

The original frozen manifests schedule **400 boots**: 71 userspace commands at N1/N4
on each emulator (284), and 46 kernel commands across their eligible CPU counts
(116). The 35 LKMM cases run at N4; only the one-thread CoWW case runs at N1.
The other 34 would execute zero instances at N1 under klitmus7's normal defaults,
so those 68 emulator/CPU combinations are explicitly ineligible, not passes.

Freeze executable/image hashes and a per-command execution manifest before
running. Preserve full serial/stdout/stderr and one result per attempt. Require
the test's own assertions and completion evidence, not merely a finished boot.
Default bounds: 300 guest seconds per command, 450 host seconds per boot;
initial torture selections use 30 guest seconds followed by orderly completion.
The extended RCU/SRCU follow-ups use 90 guest seconds, with the same outer caps.
An isolated retry may extend the bound to 900 guest / 1200 host seconds.
Every initial failure/cap remains in the record. Continue later commands after
a failure or stall. N1 affinity skips must be checked at N4. Preparation and
initial execution are bounded to four hours; remaining work must be named if
that bound is reached. No runtime bug fixes are part of this test-only request.

The shared VM lock serializes emulator runs. Kernel warnings, TAP failures,
LTP failure/broken/configuration codes and resource caps are classified
separately. QEMU agreement is supporting comparison, not hardware proof.

## Defect list

No new natemu defect was confirmed. The following list separates fixture,
guest-kernel and QEMU-control findings from remaining coverage gaps.
Existing unresolved issues remain in the [correctness backlog](correctness-backlog.md).

| Finding | Classification and disposition |
| --- | --- |
| `mremap_test`: four 2 GiB source mappings rejected in each configuration | Resolved guest fixture: kernel commitment accounting returned ENOMEM before `mremap`. All four configurations pass those cases after `vm.overcommit_memory=1`; default validation touches only 4 MiB per mapping. |
| `mremap_test`: 40 MiB preamble rejected with EEXIST | Test-layout issue: reproduced on QEMU N4 initially and natemu N1 in the overcommit retry. Upstream checks the destination range, but not the preceding preamble. With overcommit enabled and ASLR disabled, all four configurations pass all 16 positive and three expected-negative checks. Actual original conflicting VMA was not captured. |
| `cow`: missing prerequisites and one collapse condition | Local GUP-enabled kernel rerun completed on all four configurations: 434 passes, four upstream expected failures, 217 skips. All 220 formerly missing-GUP checks per run now pass. Remaining skips: 216 require swap; one fully shared THP collapse returns EINVAL. No unexpected failing COW assertion. |
| `ptrace/vmaccess`: `global.attach` hits its internal 30-second timeout | Confirmed guest Linux wait cycle, reproduced initially and in isolation on both emulators at N1/N4. All 12 read-only snapshots show `ptrace_attach` waiting while `exec` waits for an unreaped traced sibling. Independent review checks PID relationships and matching source paths; VM diagnostics and shutdown continue normally. Original and retry results remain failed. |
| QEMU `cross-modifying-code`, N4: SIGSEGV | Reproducible QEMU compatibility-stress failure: original plus three fresh repeats fail; natemu passes all four N4 attempts. Original QEMU fault is an instruction fetch at a page boundary; repeats fault on data reads at the patched function entry. The four-byte exchange is aligned and crosses neither a page nor cache line. Exact raced decoding/invalidation cause is unproven. The test deliberately omits Intel's cooperative patching protocol, so this is not an architectural ordering verdict. |

Resolved interpretation issue: `test_vmalloc` deliberately returns `-EAGAIN`
after printing its results. This is a kernel test module, not a userspace library; no missing-library
error occurred. BusyBox 1.37.0 first attempts `finit_module`, then
falls back to `init_module` on failure, causing two complete executions. The
matching Debian patch series does not change that code. All four configurations
reported zero failed vmalloc checks in both executions. The nonzero loader exit
and duplicate output are retained; neither is classified as a natemu defect or
as two independent repetitions. Exact sources are in the artifact `sources/`
directory (`busybox-1.37.0.tar.bz2`, Debian patch archive, `test_vmalloc.c`).

## Ptrace follow-up investigation

The attach timeout matches a known upstream Linux ptrace/exec circular wait;
it is killable and does not freeze the VM. The four original uninstrumented
runs and four isolated runs fail before reaching the test's errno assertion.
Revalidation of the retained diagnostics confirms all 12 PID-linked snapshots
and their output hashes:

1. The child enters `execve` while holding `cred_guard_mutex`.
2. `de_thread` waits for a traced sibling to be reaped.
3. The tracer is blocked in `PTRACE_ATTACH` waiting for that same mutex, so it
   cannot call `waitpid` to reap the sibling.

The public test explicitly creates the traced sibling. Its first attach expects
`-1/EAGAIN`, but the tested Linux 6.12.107 path blocks instead. Missing libraries,
permission rejection and slow emulation do not explain this observed wait cycle.
The read-only snapshot wrapper is not necessary to trigger it: the original
runs, without that wrapper, also timed out.

The author's [upstream v19 proposal, December 2025](https://kernsec.org/pipermail/linux-security-module-archive/2025-December/054369.html)
explicitly describes this case. It releases the exec credential mutex around
these waits while extending permission checks to account for pending exec
credentials. It also changes the test to expect successful initial attachment,
rather than the older proposed EAGAIN behavior. Changing our test expectation
alone would not unblock the syscall. No test or kernel patch was applied.

An immutable source check of upstream mainline
[`fa4ad798f6f681031775266044ba1b0cf3c670bd`](https://github.com/torvalds/linux/tree/fa4ad798f6f681031775266044ba1b0cf3c670bd)
(2026-11-07) still finds the same mutex/wait structure, the original EAGAIN test
expectation, and no proposed `exec_bprm` field. The v19 fix is absent from this
inspected revision. This is source evidence, not a runtime test of that kernel;
an upgrade is not a verified remedy. Retained source files, SHA-256 hashes and
revalidated diagnostics are in `ptrace-upstream-review/` beside the raw runs.
Independent review agrees on the guest-kernel cause and the separate obsolete
test expectation. All eight observed attach failures remain failures; neither
longer timeouts nor a natemu change is indicated by this evidence.

## Results

All selected rseq (92 boots), futex functional (60), membarrier (8) and LTP (56)
commands passed on both emulators at N1/N4. The MM, ptrace and QEMU selections
have the fully accounted failures and skips described above.

All 44 initial non-LKMM kernel boots have completed their reviewed checks: memcpy and
usercopy KUnit, atomic64, bitmap, vmalloc, four lock-torture types, and RCU/SRCU
at N1/N4 on both emulators. Vmalloc retains the loader-exit qualification above.
The 30-second RCU runs do **not** establish forward-progress coverage: that
phase reported zero completed tests. The subsequent `torture-01` batch closes
that bounded coverage gap: all four 90-second RCU configurations complete both
callback and need-reschedule forward-progress phases. All four SRCU configurations
complete callback progress; SRCU has no need-reschedule phase in this source, so
its `tested=0` counter is inapplicable rather than a missing callback result.
All eight added IRQ-save locking configurations pass with positive acquisition
counts and zero failures, including reader activity for `rw_lock_irq`.
These 16 follow-ups have no skips, caps, warnings or new defects.

All 72 LKMM boots completed, with exactly
630,000 samples per emulator (1,260,000 combined), every observed state allowed
by the retained herd model, and no forbidden observations. This is bounded
sampling, not a proof of memory-model completeness.

The one remaining fully shared THP-collapse skip is supported by the matching
Linux source: fork write-protects both COW mappings; this kernel's collapse path
requires a writable PTE and maps `SCAN_PAGE_RO` to EINVAL. The partial-sharing
cases retain writable PTEs and pass. This is a source-supported explanation,
not a traced kernel branch. Changing `max_ptes_shared` would not help because
that threshold applies to khugepaged, not direct MADV_COLLAPSE.
The initial COW runs had 211 passes and 444 skips each. Allocating eight 2 MiB
huge pages enabled seven previously skipped checks: three passed and four
reported upstream expected failures; eight further checks then reached their
missing-GUP prerequisite. No skipped case was represented as an ordinary pass.
The additional local rebuild used Apple container 1.7.1 after OrbStack had
stopped following a disk-full error. The resulting
`gup-build-01/bzImage` is Linux `6.12.107-natemu-public-gup`; its final configuration
diff is exactly `GUP_TEST=y` and the distinct release suffix. Sources are
unchanged. A 4 GiB tmpfs build directory inside a 6 GiB container avoided further
large disk allocations. Missing native-GCC and zstd preparation dependencies
were corrected; failed build logs are retained. The temporary builder was removed
after preserving outputs, reclaiming its disk space; Apple container remains installed.

The four `gup-cow-01` boots mount debugfs, verify the GUP node, allocate eight
huge pages, and rerun the unchanged COW binary. Each reports 434 passes, four
expected failures and 217 skips, closing all 220 missing-GUP checks per run.
Kernel SHA-256: `3625af6e7ef7d7ed165e2f1a1d149ecc7a659727bf9d31e02fb2338da429a171`.

No native AMD run was possible because SSH
to `ubuntu@192.168.12.10` failed with `No route to host` before authentication.

Independent source review confirms that the first two rseq programs completed
on both emulators at both CPU counts. The larger test performs one million
spinlock iterations and twenty million list iterations containing sched_yield;
its longer N1 execution was not a stall.

Preparation records are separate from test outcomes. `kernel-01` was rejected
before execution because module dependency metadata still named compressed files
after decompression. `kernel-02` packaging exhausted available storage before its
manifest was written. Those unrun images were removed; `kernel-03` uses corrected
dependency indexes and a compressed image. Independent review verified all 18
module dependency entries and the embedded overlay. The completed smoke image
was losslessly compressed, hash-verified, and retained with restoration metadata.

QEMU's executable hash/version was additionally recorded after the first
userspace boots; those early controls lack a pre-run cryptographic QEMU binary
pin. Candidate binaries, guest kernels, images and harnesses are pinned. This
qualification remains attached to the controls and is not retroactively erased.
Follow-up manifests include the QEMU pin; the preparation script now does so
for future batches too. Frozen initial manifests and results are unchanged.

## Retained audits

Independent agents reviewed test setup, suite-specific assertions, failure
diagnoses and result integrity. The parent reviewed and reran the accounting
checks. Public suite sources and emulator runtime code were not modified.

- [400-boot final accounting](/Users/liaozping/orca/natemu-raw/public-cpu-ram-20261107-01/independent/public-accounting-final.json)
- [Original 422-attempt inventory](/Users/liaozping/orca/natemu-raw/public-cpu-ram-20261107-01/independent/final-inventory.json)
- [Final 442-attempt inventory](/Users/liaozping/orca/natemu-raw/public-cpu-ram-20261107-01/independent/final-inventory-with-torture.json)
- [16 IRQ locking and extended RCU/SRCU results](/Users/liaozping/orca/natemu-raw/public-cpu-ram-20261107-01/independent/torture-audit-final.json)
- [Prior 426-attempt inventory including GUP](/Users/liaozping/orca/natemu-raw/public-cpu-ram-20261107-01/independent/final-inventory-with-gup.json)
- [Four GUP-enabled COW retries](/Users/liaozping/orca/natemu-raw/public-cpu-ram-20261107-01/independent/gup-cow-audit.json)
- [16 fixture/ptrace follow-ups](/Users/liaozping/orca/natemu-raw/public-cpu-ram-20261107-01/independent/public-supplemental-final.json)
- [Six code-patching repetitions](/Users/liaozping/orca/natemu-raw/public-cpu-ram-20261107-01/crossmod-01/independent-results.txt)
- [Ptrace wait-cycle diagnosis](/Users/liaozping/orca/natemu-raw/public-cpu-ram-20261107-01/ptrace-02/independent-diagnosis.txt)
- [Kernel assertion and LKMM audit](/Users/liaozping/orca/natemu-raw/public-cpu-ram-20261107-01/independent/kernel-oracle.json)

The 42 follow-ups are `fixtures-01` (8), `layout-01` (4), `ptrace-02` (4),
`crossmod-01` (6), `gup-cow-01` (4), and `torture-01` (16). `ptrace-01` was
rejected before execution to add bounded diagnostic reads. Five completed
harness smoke boots are separate from these 442 public-test executions.
No planned public-test row remains pending.

`torture-01` uses `spin_lock_irq` and `rw_lock_irq` for 30 seconds, plus RCU
and SRCU for 90 seconds with upstream `fwd_progress=1 fwd_progress_holdoff=1`,
on both emulators at N1/N4. The independently reviewed oracle requires actual
completed progress, positive activity, final SUCCESS and clean kernel output.
Its 11 regression checks pass. The parent reran the final audit: all 16 outputs,
19 input pins, launch identities and overlay contents match, with no foreign
VM overlap or forced shutdown. Public tests and runtime sources remain unchanged.

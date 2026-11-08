# Kernel CPU/RAM suite setup review

2026-11-07, independent source/setup review by agent `/root/suite_survey`.
No VM, build or test was launched by this reviewer. This is preparation advice;
it does not approve an unseen final runner or establish execution results.

This document preserves the preparation reviews below. Completed execution
results and later coverage additions are in the
[final campaign report](../../restructure/correctness-public-kernel.md).

## Local prerequisites verified

The retained guest configuration is
`../natemu-raw/kernel-census-20261028-01/x/img/config-6.12.107+deb13-amd64`.
It enables SMP, futex/PI, RSEQ, MEMBARRIER, DEBUG_FS, modules, unloading and
MODVERSIONS. KUnit, atomic64 selftest, bitmap, vmalloc, RCU torture and lock
torture are all disabled. Thus none can be reported as already available merely
because Linux booted. Module signature enforcement is disabled and forced
lockdown mode is NONE; normal direct-boot loading is plausible, not observed.

**An exact matching header package is cached**, despite its generic filename:
`../natemu-raw/track03-source-audit-20260919/stock/debian-headers.deb`.
Its control archive identifies `linux-headers-6.12.107+deb13-amd64`, version
`6.12.107-1`. Its data archive contains the matching `Module.symvers` and
architecture header configuration. It depends on the matching `-common` headers
and `linux-kbuild-6.12.107+deb13`, which were not found in the inspected local
file inventory. GCC 14 and make packages are already cached beneath
`../natemu-raw/bench-20261103-01/debs/`.

The cached symbol table contains RCU torture helper exports but no KUnit or
common torture-framework exports. External atomic64/bitmap/vmalloc/klitmus
modules are plausible with the complete matching header build environment.
RCU/lock torture additionally need the unchanged common `kernel/torture.c`
module and internal RCU headers from the exact kernel source. Do not force
module-version mismatches or suppress unresolved symbols.

## Smallest practical setup

If cross-building a complete kernel is now inexpensive, use exact matching
kernel source and a configuration derived from the existing guest, with only
selected tests enabled. Let Kconfig resolve dependencies and inspect the final
`.config`; do not pass ad-hoc preprocessor defines against `CONFIG_KUNIT=n`.
The framework can be modular, but enabling it affects kernel integration, so
compiling only KUnit test modules against stock headers is insufficient.
[Upstream KUnit configuration/run instructions](https://www.kernel.org/doc/html/v6.12/dev-tools/kunit/start.html).

Requested module configuration:

```text
CONFIG_KUNIT=m
CONFIG_MEMCPY_KUNIT_TEST=m
CONFIG_USERCOPY_KUNIT_TEST=m
CONFIG_ATOMIC64_SELFTEST=m
CONFIG_TEST_BITMAP=m
CONFIG_TEST_VMALLOC=m
CONFIG_RCU_TORTURE_TEST=m
CONFIG_LOCK_TORTURE_TEST=m
# CONFIG_KUNIT_ALL_TESTS is not set
```

Kconfig selects the common torture framework. Preserve required boot/runtime
options and record the configuration difference. If dropping unrelated device
drivers to reduce build work, verify the same CPU, MMU, preemption, mitigation
and SMP configuration remains; the test kernel is a distinct guest build,
not evidence that these modules ran on the unmodified Debian image.

Use exact-version test sources and framework together. Current upstream
usercopy has a `kunit_vm_mmap` helper, so copying current tests onto old headers
would create an avoidable version dependency. Usercopy assertions cover valid
copies, rejected kernel-address copies, get/put user values and failure zeroing;
its deliberately dangerous reversed-copy test is disabled upstream. Do not
enable it or infer coverage for it.
[Current usercopy source](https://raw.githubusercontent.com/torvalds/linux/master/lib/tests/usercopy_kunit.c).

## Execution and oracle checklist

| Suite | Initial bounded selection | Required result |
| --- | --- | --- |
| memcpy/usercopy KUnit | Load unchanged selected suites, one at a time; N1 then N4. | Parsed KTAP plans, individual case outcomes and suite completion. Successful module load alone is not a pass. Keep every skip. |
| atomic64 | Load `atomic64_test`; repeat with the N4 guest as a functional control. | Final passed message **and no WARN/BUG**. It checks atomic operation values; it is not a concurrent ordering test. |
| bitmap | Load `test_bitmap`. | Complete test count and zero failures; capture diagnostic output. |
| vmalloc | Start `run_test_mask=1 nr_threads=1 test_loop_count=1000 test_repeat_count=1`; N4 with `nr_threads=4`. Then separately selected mapping tests after inspecting exact-version masks. | Per-test completion/failure counts. Default loops are large. `align_shift_alloc_test` is intentionally unsuccessful; do not classify that as an emulator bug. |
| locktorture | Separate `spin_lock`, `spin_lock_irq`, `rw_lock`, `rw_lock_irq`, `mutex_lock`, `rwsem_lock` sessions, explicitly `onoff_interval=0`, bounded writers/readers. 30 guest seconds each for a first batch, then orderly unload. | Final SUCCESS, zero error flags and nonzero acquisition counts. Retain stall warnings and host wall-time caps separately. |
| rcutorture | One ordinary `rcu` session at a time, hotplug disabled; 30 guest seconds initially, orderly unload. Resource-heavy or deliberate-stall parameters excluded and recorded. | Final SUCCESS plus actual reader/grace-period progress and no assertion/stall failures. This is a bounded smoke test, not the upstream full multi-scenario torture campaign. |
| klitmus7 | Small unchanged upstream kernel litmus selection whose thread count fits N4, matching generated-module kernel headers. Run N1 only where generator accepts it as a control. | Complete outcome histogram and executed sample count for every test; compare with the selected LKMM model. Missing/capped results are not pass; no observed forbidden outcome is not proof. |

The vmalloc numeric mask above selects only the first allocation case; exact
source controls must be checked against the source finally downloaded. Torture
modules start on load and stop on unload. Do not concurrently unload and trigger
the framework's automatic shutdown. Use a guest-side duration plus a separately
recorded host timeout: emulator time dilation makes those different quantities.
[vmalloc source](https://github.com/torvalds/linux/blob/master/lib/test_vmalloc.c),
[locktorture protocol](https://docs.kernel.org/locking/locktorture.html),
[RCU torture protocol](https://docs.kernel.org/RCU/torture.html),
[klitmus7 prerequisites](https://raw.githubusercontent.com/torvalds/linux/master/tools/memory-model/README).

## Validity and relevance judgments

**Evidence validity:** source/setup checks support the above preparation, but
final exact source versions, build products, kernel configuration and result
parser remain to be reviewed. Baseline QEMU TCG and natemu should run the same
kernel/modules/commands at the same vCPU counts. QEMU agreement isolates
integration faults but does not settle disputed native architectural semantics.
No discovered natemu defect follows from this source review.

**Decision relevance:** kernel copy/usercopy, allocator mapping churn and kernel
locking add execution contexts beyond the previous userspace corpus. The new
kernel build is justified by enabling unchanged public tests, not by performance
measurement; any timing comparison using it must be identified separately.
Native reference unavailability limits architecture-oracle disputes, but does
not prevent functional assertions from finding useful defects. Maintain separate
statuses for emulator failure, upstream expected failure, missing prerequisite,
resource cap and unclassified baseline/candidate divergence.

## Retained source/oracle review after build

Reviewed `../natemu-raw/public-cpu-ram-20261107-01` source archives, generated
LKMM source, model oracle and final kernel configuration. No runtime was
launched. The five retained kernel test sources (`memcpy_kunit.c`,
`usercopy_kunit.c`, `atomic64_test.c`, `test_bitmap.c`, `test_vmalloc.c`) are
byte-identical to their entries in the retained `linux-6.12.107.tar.xz`.
The configuration enables the requested modules, KUnit by default and debugfs,
without KUNIT_ALL_TESTS. It retains SMP and voluntary/dynamic preemption.

- **KUnit counts:** memcpy has six cases: memset, memcpy, memcpy_large, memmove,
  memmove_large and memmove_overlap. The last four are `KUNIT_CASE_SLOW`; do not
  silently filter them. Usercopy has four cases: valid, invalid,
  check_nonzero_user and copy_struct_from_user. The retained stable source
  includes `kunit_vm_mmap`; the earlier concern about mixing current sources
  with older framework is resolved by using the matching source tree.
- **Bitmap:** the unchanged helper prints `all N tests passed` only when failure
  and skip counts are both zero. A skipped run uses separate `skipped N tests`
  and `remaining N tests passed` lines. Failure returns `-EINVAL`. Preserve
  this distinction and require a positive test count.
- **vmalloc version difference:** this exact source has 11 cases and default
  `run_test_mask=INT_MAX`, not the current-mainline default 7. It has no expected
  failure field; `align_shift_alloc_test` (bit 64) intentionally fails and is
  counted in its ordinary failure column. Explicitly select the intended mask.
  Assert one summary per selected case per worker, matching repeat/loop counts.

The generated LKMM tar contains 35 tests. All generated test names and hashes
match the 35 model oracle blocks exactly. The oracle contains **23 Never and
12 Sometimes**, with no Always. Those class labels describe model possibilities;
finite executions need not observe an allowed condition. In particular do not
compare observed and model class strings for equality.

| Generated thread count | Number of tests | Default instances at N4 | Samples per test at size=1000, nruns=10 |
| --- | ---: | ---: | ---: |
| 1 | 1 | 4 | 40,000 |
| 2 | 24 | 2 | 20,000 |
| 3 | 8 | 1 | 10,000 |
| 4 | 2 | 1 | 10,000 |

Expected total at N4 is **620,000 samples**, if default `ninst=0` and `avail=0`
are retained. Crucial correction: **CoWW+poonceonce (`litmus003`) is a one-thread
test**, so it is eligible at N1 and should yield 10,000 samples. The other 34
tests are N1-ineligible: the generated code computes `ninst=avail/nthreads`, then
explicitly prints `skipped` when `ninst==0` or required threads exceed online
CPUs. They cannot be reported as N1 passes. Overriding `ninst=1` does not remove
that eligibility check.

Require exactly one complete observation/hash block per expected test, positive
runtime samples and exact expected totals. The generator can return partial
histograms if thread allocation fails, so nonzero alone is insufficient.
Sum histogram counts and reconcile them with Positive+Negative. Compare observed
states against the model's enumerated state set after canonicalizing location
names/order; the pointer-publication case uses symbolic x/y values in both.
Checking only the forbidden final condition would miss other invalid states,
especially in the twelve Sometimes tests. Model witness counts are explored
executions, **not** runtime sample expectations or necessarily unique states.

This accepts the retained source selection and oracle/count interpretation for
bounded execution. Author runner/parser review is still pending separately.

## Kernel-01 launch review

Reviewed `public-prepare.py`, `public-init.sh`, `run-public.sh` and
`public-campaign.py` against the frozen `kernel-01/harness` copies; all four
match byte-for-byte. The manifest has 46 cases and 116 boots, with unique queue
identities: 11 kernel-module configurations at N1/N4 on both engines, 35 LKMM
cases at N4 on both engines, and CoWW additionally at N1 on both engines.
The generated LKMM arguments remain unchanged. Console informational messages
remain visible. Exact-version torture sources support the selected parameters;
module dependency metadata includes the KUnit and torture framework dependencies.

**Execution validity: accepted for the bounded exploratory launch.** The runner
retains complete per-boot outputs, preserves nonzero exits and does not promote
exit-zero completion to correctness pass. Per-suite oracle review remains
required before accepting results.

The exact vmalloc mask 1983 selects bits 0–5 and 7–10: all ten nonintentional
cases, excluding alignment-shift. `test_loop_count=100`, one repeat and workers
equal to online CPU count are supported. Its initializer **always returns
`-EAGAIN` after executing tests to unload itself**, so command rc 1 can be an
expected loader result. Acceptance must additionally require exactly ten
summaries per worker, each passed=1, failed=0, repeat=1 and loops=100, with no
kernel warning/Oops. Missing summaries cannot become an expected exit.

The loop bound does not constrain fixed setup allocations: full-fit at N4
allocates approximately 192 MiB per worker transiently (roughly 768 MiB total),
and per-CPU testing performs 35,000 allocations per worker. Within the configured
2 GiB guest this is plausible, but any OOM/resource failure needs separate
diagnosis. Keep original failed/capped runs before isolated reruns.

The final oracle should validate reported CPU count and cap for every result,
including expected vmalloc rc 1; the generic runner presently applies those two
identity checks only to exit-zero completed rows. The 30-second torture runs
exercise four selected lock types and rcu/srcu. They omit lock IRQ variants,
and the exact RCU default forward-progress holdoff is 60 seconds, so this batch
does not establish forward-progress torture coverage. No source patches or
runtime executions were performed by this reviewer.

### Preflight return: kernel-01 rejected before execution

The author subsequently found a packaging defect: module files had been
decompressed from `.ko.xz` to `.ko`, but dependency metadata still referred to
`.ko.xz` because depmod did not follow the relocated `usr/lib` layout. The earlier
launch acceptance was **withdrawn** for kernel-01; it was not run. This review
checked dependency relationships but initially omitted verification that every
referenced pathname existed. Kernel-01 remains a rejected preflight artifact.
Kernel-02 then failed preparation due to full storage before producing a
manifest and was not run. The first attempt to record this review return also
failed due to full storage; this paragraph records it after storage recovery.

The author corrected merged-/usr paths and regenerated metadata, then froze
kernel-03. Independent closure check of its overlay found 18 modules in
`modules.dep` and **zero nonexistent referenced paths**; every path uses `.ko`.
The KUnit and common torture dependencies resolve to existing module files.
Test sources and assertions remain unchanged.

The frozen kernel-03 image was also inspected by streaming its archive contents:
`lib` is a symlink to `usr/lib`, and all module files and final metadata live
under `usr/lib/modules/6.12.107-natemu-public`. Its second compressed frame
decompresses byte-identically to retained `overlay.cpio`, whose dependency
metadata SHA-256 is
`6463a5b1e7e6ae7779f5f3cecd882fb5ca95ed40fe98d8d53c08be62448065f5`.
The image contains adjacent base/overlay zstd frames followed by two zero bytes;
the generic zstd command rejects the trailing padding after producing output,
whereas the Linux initramfs format allows zero padding. This follows from the
writer buffering the intended separator before invoking its subprocess. It
does not invalidate the verified archive contents; a future writer can flush
before invoking the compressor to produce its intended layout. Kernel-03
packaging acceptance is restored. Runtime/oracle results remain pending.

# Additional public CPU/RAM test suites

Source survey, 2026-11-07. **No new suite was built or executed.** These are
candidate ideas, not accepted correctness evidence. Linux v6.12 configuration
and build manifests were checked where accessible; links to `master` describe
the surveyed upstream sources and must be pinned before execution. Compatibility
with the actual guest image remains untested.

That statement describes the survey stage. Subsequent execution and defect
tracking are recorded in [the public-test campaign](../../restructure/correctness-public-kernel.md).

Scope: x86-64 CPU and ordinary RAM, including kernel execution and SMP up to the
configured four-vCPU limit. Exclude x87, compat/32-bit execution, CPL1/2, nested
virtualization and device/I/O tests. Existing FEX ASM, Linux **x86** selftests,
litmus7, stress-ng, GCC torture and glibc coverage is not counted as new here.
Linux selftests outside `x86/` and actual kernel modules add different workloads;
they do not imply that the project previously had no kernel-mode tests.

## Recommended order

| Priority | Unmodified upstream candidate | Distinct coverage and oracle | Integration and limits |
| --- | --- | --- | --- |
| 1 | **Linux kernel memory-model litmus tests through klitmus7** | Kernel barriers, release/acquire, dependency and locking patterns. Compare observed outcomes with the selected LKMM model; a forbidden outcome is a failure, absence is bounded evidence only. | klitmus7 already generates kernel modules from upstream C litmus tests. Requires matching kernel build headers/config and compatible herdtools7. This is a new execution path without patching litmus7. It does not automatically cover unaligned, mixed-size, string or resident/emulated interactions. [Upstream workflow](https://raw.githubusercontent.com/torvalds/linux/master/tools/memory-model/README), [corpus descriptions](https://raw.githubusercontent.com/torvalds/linux/master/tools/memory-model/litmus-tests/README). |
| 1 | **Linux memory-copy and usercopy KUnit tests** | `lib/memcpy_kunit.c`: byte-exact copies/moves/fills and untouched surrounding bytes. `usercopy_kunit`: kernel/user boundary handling and user-copy helper results. KUnit assertions give explicit failure locations. | Ordinary Linux guest, kernel rebuilt/configured with `CONFIG_KUNIT`, `CONFIG_MEMCPY_KUNIT_TEST`, `CONFIG_USERCOPY_KUNIT_TEST`; modules require matching kernel and module loading. v6.12 calls this `usercopy_kunit`, **not** the older `test_user_copy`. Later upstream moved sources under `lib/tests/`. [v6.12 config](https://raw.githubusercontent.com/torvalds/linux/v6.12/lib/Kconfig.debug), [v6.12.46 copy tests](https://kernel.googlesource.com/pub/scm/linux/kernel/git/stable/linux-stable.git/+/refs/tags/v6.12.46/lib/memcpy_kunit.c), [current usercopy source](https://raw.githubusercontent.com/torvalds/linux/master/lib/tests/usercopy_kunit.c). |
| 2 | **Linux `rseq` selftests**: `basic_percpu_ops_test`, then bounded `param_test` selections | Critical-section restart across scheduling and CPU migration; per-CPU lock counters and list contents are checked for lost/duplicated operations. Complements static instruction tests and simple litmus outcomes. | Ordinary Linux userspace; `CONFIG_RSEQ`, pthreads, test `librseq.so`, matching test support files. The basic test itself uses 200 threads; bound host time without editing it. Current Makefile includes newer extension tests that should not be assumed to work on v6.12. [Source](https://raw.githubusercontent.com/torvalds/linux/master/tools/testing/selftests/rseq/basic_percpu_ops_test.c), [build](https://raw.githubusercontent.com/torvalds/linux/master/tools/testing/selftests/rseq/Makefile). |
| 2 | **Linux futex functional selftests**: `futex_wait_wouldblock`, `futex_requeue_pi`, `futex_requeue_pi_signal_restart`, `futex_requeue_pi_mismatched_ops` | User-memory atomics composed with kernel wait/requeue, PI and signal restart paths; functional harness results. Select these before timeout-heavy tests. | Ordinary Linux guest with futex and PI support (`CONFIG_FUTEX`, applicable `CONFIG_FUTEX_PI`), pthreads; inspect scheduling/privilege needs per selected binary. Current upstream also includes NUMA and new futex variants; do not run the whole directory blindly. [Exact build/test inventory](https://raw.githubusercontent.com/torvalds/linux/master/tools/testing/selftests/futex/functional/Makefile). |
| 2 | **Linux MM selftests**: `mremap_dontunmap` first; then `cow`, `mremap_test`, `madv_populate` after per-test resource/config inspection | `mremap_dontunmap` checks contents after page-table moves while retaining the source mapping: useful integration pressure on translation invalidation and faults. Other named targets broaden COW/remap/population paths, but their detailed compatibility was not audited here. | Ordinary Linux guest, MMU and upstream selftest helpers. Selected targets exist in v6.12. The full MM runner includes hugepage, NUMA, optional io_uring and 32-bit variants, so use an explicit binary list. Preserve feature skips. [v6.12 inventory](https://raw.githubusercontent.com/torvalds/linux/v6.12/tools/testing/selftests/mm/Makefile), [remap source and data oracle](https://raw.githubusercontent.com/torvalds/linux/master/tools/testing/selftests/mm/mremap_dontunmap.c). |
| 3 | **Linux `test_vmalloc`** | Kernel virtual mapping allocation/free/reuse and parallel allocator activity; per-test failure counters and kernel diagnostics. This is stress/allocator coverage, not a complete byte-content or memory-ordering oracle. | Ordinary guest module, `CONFIG_TEST_VMALLOC=m`, MMU and matching module build. Upstream parameters bound `nr_threads`, `test_loop_count`, `test_repeat_count`, `nr_pages` and `run_test_mask`; default loops are large. Run selected ordinary allocation/map cases within the configured guest RAM budget. Some cases intentionally fail (for example `align_shift_alloc_test`); preserve upstream expected-failure classification. [Source and controls](https://github.com/torvalds/linux/blob/master/lib/test_vmalloc.c), [v6.12 configuration](https://raw.githubusercontent.com/torvalds/linux/v6.12/lib/Kconfig.debug). |
| 3 | **Selected LTP syscall tests**: `mprotect04`; later selected futex tests | `mprotect04` checks SIGSEGV after PROT_NONE and executable mappings. LTP adds syscall error paths and process/signal compositions rather than isolated ISA semantics. | Unchanged LTP binaries inside Linux; build/install its harness and provide a writable temporary directory. `mprotect04` also uses file-backed setup, so this is not a pure RAM-only harness. Avoid blanket `runtest/mm`: it includes OOM, swapping and large stress workloads. `futex_wait_bitset01` has a 100 ms lateness threshold, so its timing failures need separate emulator-clock/scheduling analysis. [mprotect04](https://raw.githubusercontent.com/linux-test-project/ltp/master/testcases/kernel/syscalls/mprotect/mprotect04.c), [MM selection manifest](https://raw.githubusercontent.com/linux-test-project/ltp/master/runtest/mm), [futex timing oracle](https://raw.githubusercontent.com/linux-test-project/ltp/master/testcases/kernel/syscalls/futex/futex_wait_bitset01.c). |

## Secondary candidates and integration-heavy alternatives

- **Atomic64 selftest and bitmap tests:** `lib/atomic64_test.c` checks both
  atomic_t and atomic64_t results through WARN/BUG assertions; it does not establish
  inter-CPU ordering. v6.12's build manifest selects it with
  `CONFIG_ATOMIC64_SELFTEST`; check the exact guest configuration before scheduling.
  `CONFIG_TEST_BITMAP` selects functional bitmap tests, with explicit expected
  values and failure counts. These are inexpensive kernel instruction/data-path
  additions, not replacements for SMP stress.
  [Atomic source](https://linux.googlesource.com/linux/kernel/git/torvalds/linux/+/abea5c349368a683e40fdc2e745099aaaacc89d7/lib/atomic64_test.c),
  [v6.12 build manifest](https://raw.githubusercontent.com/torvalds/linux/v6.12/lib/Makefile),
  [bitmap source](https://raw.githubusercontent.com/torvalds/linux/master/lib/test_bitmap.c).
- **membarrier and ptrace selftests:** `membarrier_test_single_thread`,
  `membarrier_test_multi_thread`, `membarrier_rseq_stress` are useful follow-up
  candidates (`CONFIG_MEMBARRIER`, plus RSEQ for that variant), but source-level
  oracle and version checks remain before selection. `ptrace/vmaccess.c` is
  verified: it checks `/proc/$pid/mem` access and attach behavior during exec
  transitions using harness assertions. It requires procfs, pthreads and the
  `true`/`sleep` commands. This complements x86 debug tests but is mainly Linux
  synchronization/ABI coverage, so lower priority.
  [membarrier inventory](https://raw.githubusercontent.com/torvalds/linux/master/tools/testing/selftests/membarrier/Makefile),
  [ptrace source](https://raw.githubusercontent.com/torvalds/linux/master/tools/testing/selftests/ptrace/vmaccess.c).
- **QEMU TCG x86-64 tests:** selected standalone userspace cases can run as normal
  guest binaries without importing QEMU's execution harness. `cmpxchg.c` checks
  byte/word/dword accumulator preservation; it is not a LOCK/SMP ordering test.
  `cross-modifying-code.c` is useful translation-cache stress, but explicitly uses
  a cooperation pattern different from Intel's prescribed sequence; do not treat
  its success/failure as an architectural ordering verdict. Audit ISA prerequisites
  and retain upstream output/reference rules before adoption.
  [cmpxchg source](https://github.com/qemu/qemu/blob/master/tests/tcg/x86_64/cmpxchg.c),
  [cross-modifying-code source](https://github.com/qemu/qemu/blob/master/tests/tcg/x86_64/cross-modifying-code.c).
- **Cyberus guest-tests:** independent privileged CPU tests are attractive, but
  these are mini-kernels, not Linux programs. Their fixed-address ELF payloads
  load at 8 MiB and boot via Multiboot1/2 or Xen PVH; the platform assumes
  LAPIC/IOAPIC/PIT/serial. An external loader/console adapter and per-case scope
  inspection are needed before claiming unchanged binaries work. Its CPUID test
  also checks an allowed brand-string prefix list. Lower priority than Linux
  modules and the separately proposed kvm-unit-tests integration.
  [Upstream boot and platform contract](https://github.com/cyberus-technology/guest-tests#binary-format-supported-boot-flows-limitations-and-restrictions).

## First bounded batch

Prepare unchanged upstream **memcpy/usercopy KUnit**, a small **klitmus7 kernel
corpus**, and **rseq basic_percpu_ops_test** at N1/N4. Then add selected futex and
MM binaries. Keep source versions, kernel config, command lines, resource caps,
individual outcomes and complete logs. A cap, prerequisite skip or stalled VM is
not a pass and must remain a separate result requiring rerun or disposition.

Use upstream assertions as the primary oracle; QEMU comparison helps isolate
failures but is not a hardware specification. Disputed exception priority,
partial effects and instruction behavior still need the applicable architecture
contract and, where already identified as unresolved, native x86 reference runs.
These suites broaden coverage; they do not close every discovered gap or replace
the existing focused regressions. No test-suite source patches are proposed.

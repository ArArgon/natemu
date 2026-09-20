# Track 02 external-source triage — September 17, 2026

Candidate assessment by `/root/external_research`; source-only, no experiments.
This note is outside the review workflow and is not accepted research evidence.
Read the current workflow, feasibility charter, correctness charter and Q-02
backlog; did not inspect archived history. Upstream moving pages were inspected
on the date above; pin revisions before adopting test code.

**Inferred:** Most required behavior and many test ingredients already exist.
What remains unanswered is whether the selected translator, shim and machine
integration implement them. Existing sources should reduce the research to a
contract/coverage audit and tests of the integration differences, not a fresh
discovery of x86 semantics or a new general-purpose conformance suite.

| Source-backed resource | What is already available | Inferred residual and limit |
| --- | --- | --- |
| [QEMU testing documentation](https://www.qemu.org/docs/master/devel/testing/main.html), sections “Testing with make check-tcg”, “QTest”, “Other TCG Tests” | Guest instruction smoke tests for linux-user and softmmu; QTest device testing through a special QEMU protocol; pointers to kvm-unit-tests and LTP. | Reuse individual cases and compare implementations. QEMU explicitly calls check-tcg smoke testing; it is not a complete ISA conformance oracle. QTest harness integration is QEMU-specific. LTP primarily checks the Linux syscall interface, not complete privileged CPU semantics. |
| [kvm-unit-tests README](https://github.com/kvm-unit-tests/kvm-unit-tests/blob/master/README.md) and [x86/unittests.cfg](https://raw.githubusercontent.com/kvm-unit-tests/kvm-unit-tests/master/x86/unittests.cfg) | Existing cases include APIC/IOAPIC with multiple CPUs, page access, event injection, IDT, MSRs, TSC, XSAVE and PCID. The runner supports accelerator selection; configurations specify feature profiles and some restrict tests to KVM. | Strong source of privileged-system regression cases. Select only exposed features and audit each oracle. Port the payload entry, result transport and machine assumptions to the direct-boot shim; no evidence here that binaries run unmodified. VMX/SVM and other unexposed features are not obligations. |
| [FEX project README](https://github.com/FEX-Emu/FEX) and [assembly harness documentation](https://wiki.fex-emu.com/index.php/Development:InstCountCI) | Existing x86 application translator for ARM64 Linux and an assembly TestHarnessRunner with IR/disassembly inspection. | Candidate reusable instruction fixtures/code generation expertise for Q-03-02. Application compatibility does not answer full-system paging, privileged state or precise guest exception delivery. The cited InstCountCI checks translation size/regression, not semantic conformance. An actual coverage/oracle audit remains necessary. |
| [Box64 usage documentation](https://github.com/ptitSeb/box64/blob/main/docs/USAGE.md), BOX64_DYNAREC_TEST, BOX64_DYNAREC_DIV0, BOX64_DYNAREC_X87DOUBLE, BOX64_PROFILE | Built-in interpreter-versus-dynarec comparison of register/memory results; documented correctness/performance settings, including division exceptions, x87 handling and unsafe optimization profiles. | Reuse comparison machinery and audit exact settings. Its own interpreter is not automatically an independent oracle. Application defaults are not automatically suitable for a system emulator. Documentation itself supports keeping floating-point, exception and profile selection questions open. |
| [Linux 6.12 x86 boot protocol](https://docs.kernel.org/6.12/arch/x86/boot.html), 64-bit Boot Protocol | Documented 64-bit entry, boot_params, paging, mappings, segment and register prerequisites. | The entry contract is source work, not an experiment to rediscover. Correct construction of a particular stock-image boot environment and its CPU/device feature requirements remain integration questions. |
| [x86-TSO research and tools](https://www.cl.cam.ac.uk/~pes20/weakmemory/index3.html) | Formal operational and axiomatic models plus litmus-based experimental comparisons and tools calculating allowed outcomes. | Derive forbidden outcomes from an explicit model rather than voting among emulators. Model applicability must match instruction/memory classes; it does not establish the shim's TLB invalidation, exception, device or generated-code publication behavior. Finite stress tests cannot establish all possible SMP executions. |

Apple's [Rosetta TSO documentation](https://developer.apple.com/documentation/virtualization/accelerating-the-performance-of-rosetta)
already documents the TSO capability used by the current feasibility charter.
The web renderer exposed little text here; the search excerpt confirms TSO
support, but this audit did not revalidate the full control contract. **Inferred:** Do not reopen the existence of that
capability as Q-02-02. Test the mapping of translated accesses and atomics to it,
and integration properties not guaranteed by switching it on.

**Inferred question dispositions:**

- **Q-02-01:** pertinent as a finite coverage matrix and performance-eligibility
  policy. Source/suite inventory first; broad semantic execution waits for a
  selected implementation. Avoid requiring every listed technique for every
  area regardless of relevance.
- **Q-02-02:** pertinent for translator ordering/atomics and cross-vCPU
  integration. Existing model/litmus material substantially reduces oracle
  design; it does not answer implementation correctness.
- **Q-02-03:** primarily specification/profile selection, followed by integration
  validation. Already specified interfaces should be cited; exact choices and
  feature consistency remain project decisions. Separate that contract from
  mechanisms such as the proposed timer scaling/layout.
- **Q-02-04:** retained containment requirements are pertinent. Bound faults to
  the actual process/device/translator design and named retained properties;
  upstream VM or translator success does not establish this composition.

**Bounded next step:** inventory the four Q-02 questions into existing source
contracts, reusable tests with dependencies, current accepted Track 01 coverage,
and project-specific gaps. Stop once each obligation has an owner and a test or
explicit deferred limitation. This can change the size and sequencing of Track
02 without authorizing implementation or a test campaign.

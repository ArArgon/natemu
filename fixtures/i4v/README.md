# I4-V reused x86 cases

`cases.json` pins four adapted FEX cases to revision
`48d71752e2a363d5535181f803b0b0e9666c1825`. Exact upstream files and the MIT
license are retained under `upstream/FEX/`. Each case records the original
expectation, adaptation rationale, source bytes/hash, instruction boundaries,
and memory effects. The `.s` files assemble with Clang's integrated assembler;
the explicit 32-bit moffs bytes preserve the upstream encodings.

The 64-bit cases accept data/stack addresses through initial registers, allowing
the same instructions to execute with low or upper-canonical addresses and a
separately selected guest RIP. The 32-bit cases use owned RAM at `0x100000`, flat
segments and A20 enabled. They test partial loads/stores with real memory access;
they do not establish segmentation, A20 switching or mode-transition coverage.

`expectations.py` computes architectural state after a successful instruction
prefix without importing the emulator or producer. A recovery check supplies
the number of instructions committed before the faulting instruction. It does
not decide which address should fault or which concurrent outcomes are legal.
Those checks belong to the I4-V runner. Flags and untouched registers/memory
must remain unchanged; compare only the low 32 GPR bits for legacy-mode cases.

Run the fixture checks with:

```sh
python3 -m unittest discover -s fixtures/i4v -p 'test_*.py'
```

Boundary addresses in these host-side checks exercise the expectation helpers,
not the runtime's MMU. A fixture definition or passing expectation self-check is
not a translated-execution result. FEX output is the system under test; FEX
execution is not an independent oracle for these cases.

The mixed paging-mode concurrency driver additionally combines the retained
`Primary_A0.asm` dword store/read and ECX-copy expectations with the
register-indirect access form in `Primary_A2.asm`. Its six-byte adapted source is
`89c1890b8b02` (`mov ecx,eax; mov [ebx],ecx; mov eax,[edx]`). Replacement source
`89c189138b02` stores EDX instead, making old-code entry observable. This is a
new combined fixture and source-coherence control, not an imported upstream
concurrency test. The independent semantics and complete physical-memory history
are checked by `tools/check_i4v_mixed.py`; the existing four-case prefix model
remains unchanged. The mixed driver uses flat segments/A20-on with distinct
per-owner operand spans and old/new physical-page read sentinels.


The V3 control campaign records two additional adaptations in the manifest's
`validation_adaptations` section. Its generated scalar instructions reuse the
retained FEX `Primary_A0.asm` / `Primary_A2.asm` expectations. The separate
`tools/i4v_controls_check.py` oracle owns the new control-transition programs;
the existing four-case prefix API remains unchanged.

For write protection, `kvm-unit-tests` revision
`734fe13536d0fc41185723c63bde17ec6364fa1d`, `x86/access.c`, supplies the actual
`check_toggle_cr0_wp` case and its permission expectation. Exact source excerpts,
line ranges, hashes, package copyright and GPLv2 license are retained under
`upstream/kvm-unit-tests/`. These excerpts are provenance, not linked runtime
code or a standalone imported suite. The adapted V3 profile first writes through
a read-only guest PTE with supervisor WP=0, then restores WP and requires a
precise protection fault with the earlier write intact. It replaces the upstream
KVM/VMX and NX-toggle harness with actual guest control instructions and the
resident/FEX boundary. Newly authored CR3, invalid-control and next-fetch cases
are not attributed to that upstream test. No SVM VMRUN control-state test is
claimed as reused MOV-CR execution coverage.

# Findings from migrating the tests to testkit (November 2026)

Moving every `tools/run_suite.py` step to testkit (docs/testing.md, plan docs/superpowers/plans/2026-11-18-full-migration.md)
reran each check from rebuilt inputs, with pinned tools and independently chosen references. This note records what that
turned up about natemu and about the references. Test-side defects found on the way were fixed in the same work and are
listed last; natemu bugs are recorded here, not fixed.

## natemu: suspected bugs

| Step / case | Host | Observed | Reproduce |
| --- | --- | --- | --- |
| `i8-faults-m2a`, case `chain3-movaps-mis` | M2 Max (no AFP), accurate FP | "cut run did not reach its hlt: watchdog resident pc=0x8000400cf210 shadows_asked=[] retries=0"; xmm and ymmh changed at the fault. 845 of 846 cases pass. | `NATEMU_FP=accurate tools/with-env python3 tools/run_i8_faults.py --fexc FEXC --out OUT` (the step's argv in tools/run_suite.py) |

## References: where they are wrong and what replaced them

- **QEMU 7.2 (user mode, the pinned corpus reference):** besides i8b's seven known two-NaN departures, `i8-forms-044` of
  the i8 corpus is one too: the offline model (tools/models/afp_fp) gives natemu's accurate-mode result with the x86 rule
  and QEMU's with QEMU's rule. Its answer now comes from real x86.
- **The AMD box (Ryzen 7 8845HS) is not a reference for two-NaN choices:** for i8b's `i8-forms-045` (VHADDPS, VMAXPD),
  `i8-random-097` (VFMADD231PD, VFNMADD132PS) and `i8-random-111`, the Ryzen returns a different NaN than the Intel SDM's
  rules (the model), which natemu follows in both FP modes. These three take the model as their reference; i8b's other
  four departures agree between the Ryzen and the model and keep x86 answers.
- **SSE4.2 PCMPxSTRx:** QEMU and the Ryzen agree on all 64 programs of the i6b corpus.
- **QEMU system mode was unpinned:** Homebrew's `qemu-system-x86_64` is 11.1.3 on the M2 Max and 11.1.2 on the M4, while
  the runners' known-departure lists assume 11.1.2. testkit now builds 11.1.2 from its signed release tarball.

## Test-side defects fixed

- Four `natemu-abi` fptrap tests compared with the M4's FPCR.AH/FIZ/NEP, which a host without FEAT_AFP ignores: they are
  `#[ignore]`d and run by the `rust-afp` suite only where AFP exists.
- `k2-stmxcsr` expected MXCSR.DE where the documented `masked-sse-de` deviation applies (fast mode, no AFP): that one bit
  is now excluded there, with the deviation named.
- The FP deviation tags (tools/models/afp_fp/predictions.json) predated the current i8 corpus: regenerated from the model
  alone (38 tags in -01, 63 in -02); all fast-mode failures on the M2 Max are documented NaN deviations.
- `test_fp_deviations` and `run_slice1_vm` resolved the corpus folder's symlink and looked up the wrong tags.

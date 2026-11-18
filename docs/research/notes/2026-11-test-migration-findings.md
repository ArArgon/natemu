# Findings from migrating the tests to testkit (November 2026)

Moving every `tools/run_suite.py` step to testkit (docs/testing.md, plan docs/superpowers/plans/2026-11-18-full-migration.md)
reran each check from rebuilt inputs, with pinned tools and independently chosen references. This note records what that
turned up about natemu and about the references. Test-side defects found on the way were fixed in the same work and are
listed last; natemu bugs are recorded here, not fixed.

## natemu: suspected bugs

| Step / case | Host | Observed | Reproduce |
| --- | --- | --- | --- |
| `i8-faults-m2a`, case `chain3-movaps-mis` | M2 Max (no AFP), accurate FP | "cut run did not reach its hlt: watchdog resident pc=0x8000400cf210 shadows_asked=[] retries=0"; xmm and ymmh changed at the fault. 845 of 846 cases pass. | `NATEMU_FP=accurate tools/with-env python3 tools/run_i8_faults.py --fexc FEXC --out OUT` (the step's argv in tools/run_suite.py) |

Load-sensitive, not settled as bugs: both failed only while several agents loaded the M2 Max, and both passed on the first
attempt in the full gate on the idle machine (run 20261118-045129-009e). A test that needs an idle machine should say
so in its claims; whether either also hides a missing natemu path is open.

- `kicks` (`run_slice3_vm.py kicks --kicks 1000 --cpus 1 2 4`), check `adversarial`: "no stop at ['resident']" (once
  also 'rearms'): the per-place stop counter for `resident` stayed 0 (entry 14,550, exit 19,361, irq_sgis 28,678). Failed
  in every seeding attempt (4 of 4, plus 2 earlier), passed on the rerun of the locked run. The case holds an exclusive
  claim, but other agents' QEMU reference containers and builds ran outside the scheduler. The check needs a peer's
  doorbell pending exactly as a vCPU enters the resident; with `--reps 1` it gets two multi-vCPU runs.
- `l4b`, case `cases/frozen` (a 100 µs PIT/IRR window over 20,000 trials): "reached rbx=1, want 0" on both attempts of
  one seeding run with all 8 performance cores busy; passed in the other seeding run and in the locked run.

## Reference infrastructure

- **Rosetta refuses the QEMU binary under load.** In Apple `container`'s amd64 containers, executing the pinned
  qemu-x86_64-static fails with "Permission denied" (exit 126) in bursts when many reference containers run at once (42 of
  200 executions in one container under load, none idle); once a container starts refusing, later jobs in it fail too.
  `ref.py` now gives each container a private copy of the binary and reruns refused jobs in fresh containers, backing off
  between attempts. A refusal is never a result: no program exits 126.

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

- **Rosetta is not a reference (checked November 18):** the 1,374 straight-line programs of the programs and fp families,
  run through Rosetta in an amd64 container (`tools/reference/ref_native.py --rosetta`), agree with the locked answer in
  451, differ in 461 and crash in 462. Crashes are SIGILL for instructions it lacks (SHA, ADX, XSAVEC, the FS/GS-base and
  66-branch corpora, parts of i9/i10/rex and the AVX corpora). In the integer fixtures every difference is RFLAGS.AF
  (bit 4): Rosetta leaves the auxiliary carry flag clear where x86 sets it. Its ELF loader also needs the first PT_LOAD at
  file offset 0, which Linux and QEMU do not (`rosetta_elf` adapts the harness ELF). Usable for quick experiments, not
  for answers.

## QEMU and the AMD box compared (November 18)

`tools/natemu-test xref tests/programs tests/fp --natemu` ran each of the 1,369 programs of both families under the pinned
QEMU 7.2 and natively on the Ryzen 7 8845HS, and natemu on every case where the two differ. Each difference was traced to
its first diverging instruction by running prefixes of the program (the first k instructions plus the final register dump)
on both. natemu in accurate mode agrees with the locked answer in every case; no natemu bug came out of it.

| Corpus | Programs | Differ | Why |
| --- | --- | --- | --- |
| fix, seed, i9, i9-big, i10, rex, i7, flow | 491 | 0 | Integer, flags, addressing, REX, branches, CPL: the two agree exactly. |
| i8 (SSE/AVX) | 246 | 10 | 8 are RCPPS/RSQRTPS/RSQRTSS approximations; 2 are two-NaN choices (below). |
| i8b (SSE/AVX) | 336 | 20 | 16 are approximations; 4 are QEMU's known two-NaN departures (`i8-forms-001`, `i8-random-067`, `-077`, `-126`, already answered from x86). |
| i6b (x87, SSE4.2) | 112 | 36 | FSIN/FCOS/FSINCOS/FPTAN results 1 to 26 ulps apart (double results); one FPREM status word. |
| adx | 56 | 49 | QEMU mishandles the 32-bit forms of ADCX/ADOX: every divergence is the first 32-bit ADCX or ADOX, where a cut program crashes QEMU (SIGTRAP); 30 whole programs crash it too. |
| sha, xsave | 116 | 116 | QEMU 7.2 lacks SHA and XSAVEC (SIGILL). |
| br66 | 12 | 12 | The Ryzen honours a 66 prefix on near branches (16-bit target, then a crash); Intel ignores it (SDM), as natemu does. |

What each kind means for the references:

- **Approximations (RCPPS, RSQRTPS, RCPSS, RSQRTSS: 24 cases).** The SDM bounds only their relative error (1.5 × 2⁻¹²);
  the exact bits are each processor's own table. QEMU returns the correctly rounded 1/x and 1/√x, the Ryzen its own
  approximation (low mantissa bits differ), and Intel parts have yet another. The locks take QEMU's value, which natemu
  reproduces in accurate mode: valid under the SDM but bit-identical to no real processor.
- **Two-NaN choices.** QEMU departs from the SDM's rule in the four i8b cases above and in `i8-forms-044` (already from
  x86); the Ryzen departs from it in `i8-random-058` (HADDPD keeps the other NaN), where the SDM model gives QEMU's answer,
  as for the three i8b cases already on the model.
- **x87 transcendentals.** The Ryzen's FSIN/FCOS/FPTAN differ from QEMU's softfloat in the last bits (FPTAN the most); no
  reference is exact here. x87 is answered from FEX's reduced-precision result for now (owner decision), which matches
  QEMU in 16 of these cases. FPREM on a quiet NaN and zero sets C3 and C0 on the Ryzen, clears them in QEMU.
- **ADX.** QEMU is wrong; the corpus is already answered from x86, which natemu matches.

## Test-side defects fixed

- Four `natemu-abi` fptrap tests compared with the M4's FPCR.AH/FIZ/NEP, which a host without FEAT_AFP ignores: they are
  `#[ignore]`d and run by the `rust-afp` suite only where AFP exists.
- `k2-stmxcsr` expected MXCSR.DE where the documented `masked-sse-de` deviation applies (fast mode, no AFP): that one bit
  is now excluded there, with the deviation named.
- The FP deviation tags (tools/models/afp_fp/predictions.json) predated the current i8 corpus: regenerated from the model
  alone (38 tags in -01, 63 in -02); all fast-mode failures on the M2 Max are documented NaN deviations.
- `test_fp_deviations` and `run_slice1_vm` resolved the corpus folder's symlink and looked up the wrong tags.

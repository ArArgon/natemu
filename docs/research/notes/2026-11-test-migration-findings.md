# Findings from migrating the tests to testkit (November 2026)

Moving every `tools/run_suite.py` step to testkit (docs/testing.md, plan docs/superpowers/plans/2026-11-18-full-migration.md)
reran each check from rebuilt inputs, with pinned tools and independently chosen references. This note records what that
turned up about natemu and about the references. Test-side defects found on the way were fixed in the same work and are
listed last; natemu bugs are recorded here, not fixed.

## natemu: suspected bugs

| Step / case | Host | Observed | Reproduce |
| --- | --- | --- | --- |
| RCPPS/RCPSS/RSQRTPS/RSQRTSS on SDM-fixed inputs: i8 `i8-forms-057`; i8b `i8-forms-031`, `i8-random-097`, `-103`, `-111` (fp family, both FP modes where not excluded) | M2 Max | natemu returns QEMU's correctly rounded value where the SDM fixes the result: a denormal source gives a finite number (RSQRT of 0x00000001 gives 0x64b504f3, RCP of 0x0000004e gives 0x7f7fffff) where the SDM treats it as 0.0 and gives infinity, and RCP of 0x7f7fffff gives the denormal 0x00200001 where the SDM flushes the tiny result to 0.0. The Ryzen follows the SDM in all of them. Found by the approximation judge (tools/models/afp_fp/approx.py); the hardcoded `approx-special` test (tools/run_approx_special.py) adds RSQRT of a negative denormal, which gives the default NaN where the SDM gives -infinity (both modes). **Fixed** in 124f9003 (branch `claude/rcp-rsqrt-sdm-fc43e0`): integer words around FEX's exact op in the fexc shim (tools/fexc/shim/fexc_sites.inc, FexcApproxScalar) give the SDM's results in both FP modes, with and without AFP and under every DAZ/FTZ/rounding control; `approx-special` also runs every form with one register as source and destination, and the known-bug entry `rcp-rsqrt-sdm-fixed-results` is gone. Cost on the M2 Max: about 2 ns more per RCPPS (14.4 to 16.5 ns in a dependent chain, most of it the FPSR save/restore that was already there); the guest benchmarks `cray`, `cray-strict` and `avx` (x264, the only one with RCP: 4 sites) are unchanged within noise (testkit bench tier, two runs each). | `tools/natemu-test run tests/fp --fresh -k "i8-forms-057 or i8-forms-031 or i8-random-097 or i8-random-103 or i8-random-111"` |
| `i8-faults-m2a`, case `chain3-movaps-mis` | M2 Max (no AFP), accurate FP | "cut run did not reach its hlt: watchdog resident pc=0x8000400cf210 shadows_asked=[] retries=0"; xmm and ymmh changed at the fault. 845 of 846 cases pass. | `NATEMU_FP=accurate tools/with-env python3 tools/run_i8_faults.py --fexc FEXC --out OUT` (the step's argv in tools/run_suite.py) |

The same stop, "watchdog resident pc=0x8000400cf210 shadows_asked=[] retries=0", before the guest reached its code page
(0x100000 not accessed), failed four fp cases on the M3 (AFP), accurate FP, while a campaign smoke run shared its four
performance cores (load average about 30): `i8-addr-05`, `i8-random-040`, `i8-flags-29` and `i8-forms-053`, all
`[afp=True,fp=accurate,max_block=0]` (run 20261119-014037-fecb; 2,966 passed). All four passed at once on the idle M3
(run 20261119-020538-6ffe).

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

## The M1 performance check through the framework (November 18)

`m1-perf` (tests/bench, `tools/run_m1_perf.py full`, every input built on the M2 Max) ran once, 1 h 04 min. Set A
(baseline `ecec5d77` against the current build, raw disk, 5 against 5): PASS, aim met, geometric mean 1.00 (1.02
uncorrected), every job inside its noise. Set B (raw against preallocated qcow2): PASS, aim met, 1.00 (1.04). Set C
(overlay, reported only): 0.995 (1.02); precondition/bw 0.949 is outside its 4.3% noise, fsync-4k-q1/fsynclat 0.769
inside its 52%. Set D (IO5) has no numbers: the raw run failed on a framework bug (a read-only clone of the IO5
inputs, fixed), the overlay run on the guest's CPython `test_timerfd_ns_TFD_TIMER_ABSTIME` (1.28 ms against 1 ms, the
timing-sensitive timerfd case; every disk check passed). The verdict reads FAIL for those two runs only. Not rerun
(30 min of exclusive machine time); the case resumes from its kept results folder, so finishing D runs only those two.

## Reference infrastructure

- **Rosetta refuses the QEMU binary under load.** In Apple `container`'s amd64 containers, executing the pinned
  qemu-x86_64-static fails with "Permission denied" (exit 126) in bursts when many reference containers run at once (42 of
  200 executions in one container under load, none idle); once a container starts refusing, later jobs in it fail too.
  `ref.py` now gives each container a private copy of the binary and reruns refused jobs in fresh containers, backing off
  between attempts. A refusal is never a result: no program exits 126.

## The M4 as a test host (gate of November 18)

- **Homebrew's Python cannot serve other processes there.** A TCP server run by `/opt/homebrew/bin/python3` (3.14) accepts
  no connection from another process (curl, nc and another Python time out), while the same server under
  `/usr/bin/python3` works and an in-process client works; the firewall is off. The framework's environment is built
  from the `python3` first on PATH, and net-linux's host fixtures run with it, so net-linux and net-linux-pci fail there
  before any guest runs. Most likely the macOS Local Network permission of Homebrew's Python app, which an ssh session
  cannot grant: a setting on that Mac, not natemu.
- **Disk nearly full** (95%, 11 GiB free after the gate's builds): the qcow 64 GiB write-zeroes test took 13 s beside
  nextest's other disk tests and 0.04 s alone; it now runs alone in `rust-timed`.

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

## The x86-TSO litmus campaign through the framework (November 19)

The litmus image was rebuilt from pins on the x86 machine (`tools/correctness/recipes/litmus`; herdtools7 7.58 and its
catalogue, litmus-tests-x86 a6dadce; 1,318 tests, herd7's oracle for every one). Part 0 of 2 (659 tests, the suite's
smoke part) ran once on the M2 Max at 4 vCPUs (run 20261118-235104-6441, guest 2,213 s): every test passed, **no
state x86-TSO forbids was observed**. Weaker evidence, as in docs/restructure/correctness-x86-litmus.md: 114 of 6,734
allowed states never appeared (68 tests), and 42 of 166 exists-tests whose condition x86-TSO allows never showed it,
all 4-thread ltx4 tests. Part 1 was not run. Reproduce at that full size (the compiled -r 100 -s 100000; without
`TESTKIT_PART_ARGS=` the suite's smoke `part_args`, 1/100 of the iterations, apply):
`TESTKIT_PART_ARGS= tools/natemu-test run --tier campaign tests/correctness/family.toml -k "litmus and part-0"`.

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
  reproduces in accurate mode: valid under the SDM but bit-identical to no real processor. Each processor's table is
  fixed: the 24 programs gave the same bits in three runs on the Ryzen (November 18). Since November 18 the 60 i8/i8b
  programs that use these instructions are judged by the SDM's rules (`judge = "approx"`, tools/models/afp_fp/approx.py)
  instead of the locked bits: any result within the bound passes (ready for an Arm reciprocal-estimate lowering), and
  the SDM's fixed results (infinity for 0.0 and denormal sources, the default NaN for negative RSQRT sources, 0.0 for
  tiny RCP results) are required. The Ryzen's results pass all 60 but `i8-random-097`, its known NaN-choice departure.
  The SDM prints the tiny-result thresholds with the directions reversed; the judge reads them as the arithmetic
  requires (sources up to 1.11111111110100000000000b * 2^125 give a normal result, from
  1.00000000000110000000001b * 2^126 the result is 0.0, either in between).
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

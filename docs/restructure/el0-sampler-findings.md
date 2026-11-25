# Where translated-code time goes: the EL0 sampler's first profile round (findings)

November 23, 2026, `m2-sampler` at a7cd8e82, all on the M2 Max (machine C2CACF57), one round: chunk 4 of the
[EL0 sampler plan](el0-sampler-plan.md). Measurement only: nothing in natemu changed. Profiles, not timings: no number
here goes on the [benchmarks page](../benchmarks.md), and no table mixes machines.

What ran (every case through `tools/natemu-test`, one run each, outputs kept):

| Run | Cases | Result |
| --- | --- | --- |
| 20261123-213409-4d92 | `guest-{build,hackbench,luajit,sqlite,zstd,startup32,scimark32-sse2}-sample`, `guest-build-sample-4`, `guest-sampler-check-sample`, `guest-sampler-cost` | all measured; `guest-sampler-cost` **failed** (below) |
| 20261123-213500-1086 | `io5-solo32-profile` (unperturbed buckets), `io5-solo32-sample` | both passed |
| 20261123-213501-54d7, 20261123-213509-3f8d | `guest-coldtrip-profile`, twice (its rounds are fixed at 3 in `coldtrip.c`) | both passed |

The census is not rerun: the guest numbers are the `census` suite's cases (`guest-<b>-census`, QEMU with the census plugin,
M2 Max, run 20261123-183251-bd87 at 9aaeb015), reduced again with `census_report.py` as of a7cd8e82 (the top-1/top-2
reduction, 2f88671c, came after that run; QEMU's counts did not change). **`io5-solo32-census` was not run** (the
orchestrator's call): its only decision input is the indirect plan's step 2 (the inline cache), which is deferred. It is
to run only if step 2 comes up.

The round took 52 minutes of machine time (21:32 to 22:24), shared with other worktrees' unit runs.

## 1. The limits first

- **The sample bell arrives 1.5 us or more after the ring, and that biases the kind split.** A sample whose ring time is
  before the vCPU's last resume is counted "resident or host" (plan section 3). Over the resident-kind samples of
  `guest-hackbench-sample`, `guest-sqlite-sample` and `guest-scimark32-sse2-sample`, the time from ring to interrupt has a
  median of 1.5-2 us and is never under 1 us; of the EL0 samples, under 0.2% are less than 1 us after a resume (an
  experiment: those cases' `.samples` read with the report's reader, `since` of each kind). So any trip that starts within
  about 1.5 us after a ring turns an EL0 sample into a resident one, and the EL0 samples kept come from EL0 stretches
  longer than that. Where trips are rare the sample split matches the buckets; where they are frequent it does not:

  | Case, phase | EL0 % of samples | guest bucket % of busy (same run) |
  | --- | ---: | ---: |
  | `guest-scimark32-sse2-sample` run1 | 92.6 | 95.0 |
  | `guest-zstd-sample` run1 | 91.3 | 91.2 |
  | `guest-luajit-sample` run1 | 67.3 | 71.3 |
  | `guest-build-sample` run1 | 47.7 | 55.2 |
  | `guest-sqlite-sample` run1 | 53.1 | 73.1 |
  | `io5-solo32-sample` make / i386-make | 58.2 / 63.8 | 68.1 / 71.9 |
  | `io5-solo32-sample` configure / i386-configure | 6.2 / 6.7 | 37.0 / 39.4 |
  | `guest-hackbench-sample` run1 | 3.2 | 38.4 |

  (EL0 % is of all samples with halted ones included; the bucket share is of busy time. They agree where halts are rare.)
  For configure and hackbench the EL0 tables below describe a small, long-stretch part of EL0 time and are not used for
  a verdict; for build, make and the benchmarks they describe most of it. The EL0/resident split is read from the
  `-profile` buckets, never from samples. A fix (not made here): record when the vCPU last *left* EL0 as well as when it
  resumed, and count a ring between those two as resident, a ring before the exit as "EL0, place unknown". Built since:
  [the ring-time fix](#the-ring-time-fix-november-24) below.
- **Sampled runs read low on guest time.** `guest-sampler-cost` this round: `build`'s guest share 1.4-1.9 points lower
  sampled, `hackbench` 0.4-0.8, `build` at 4 vCPUs 1.5-2.0 (all within the 2-point bound). Its wall bound failed for `build`
  at 4 vCPUs: run1 0.585 s slower against a bound of 0.556 s (5.443 against 4.858 s), run2 0.171 s against 0.171 s; `build`
  at 1 vCPU was 6-8% slower sampled and `hackbench` 4%, inside their bounds. The case failed; nothing was changed for it.
- **Skid.** In the validity case's chase phase (`guest-sampler-check-sample`) 62% and 61% of the loop's samples (runs 1
  and 2) fall on one word, the one before the slow load (chunk 0 saw 58%). The report gives every role share raw and
  under a one-word correction that moves *every* sample to the next word executed. That over-corrects: only about 60% of a
  slow load's samples land on the word before it, and samples on cheap words move too. The true shares lie between the raw
  and the corrected figure; both are given.
- **The census runs under QEMU**, with more timer interrupts than natemu's guest sees, at 1 vCPU, run 1 of each benchmark.
  It counts the guest program; it says nothing about natemu's exits (checked links are not in it).
- **Symbols as implemented**: kernel symbols from the booted bzImage's kallsyms (no module names: `kernel:module`,
  unmatched text `kernel:?`); user symbols only where the bench image's binaries have `.dynsym` or `.symtab` names (glibc
  is stripped: its AVX2 code is counted by proxy, blocks holding a VEX.256 or EVEX instruction); io5 user samples stay at
  block level (`user:?`). Native words come from the objects file; samples on words no object covers count as `none`.
- **Trip counts per io5 step are estimates.** The `perf` log gives each bucket's entry count only for the whole launch
  (607.5 M trips in the use launch, exact: the guest bucket's entries equal the entry kinds' summed). Per step each entry
  kind's bucket time is divided by its launch-wide mean cost per entry (link 428 ns, miss 127, helper 132, abort 519): a
  scratch reduction of `io5-solo32-profile`'s log, assuming a kind's mean cost does not change by step.

### The ring-time fix (November 24)

The profile resident now records each trip out of generated code at the start of `finish` (the first Rust the gates
call; `gates.S` is unchanged) and keeps the last 16 stretches of generated code, each its resume and its exit time. A
sample's ring time is classed among them: in the stretch the bell interrupted, EL0 at the PC (as before); between two
stretches, resident or host; in an earlier stretch, a new kind, **EL0 with no place** (the vCPU was in generated code, but
the PC the interrupt saw is a later stretch's). The report counts it as EL0 time (`el0_all_pct`) and leaves it out of
every table of EL0 places, roles and symbols. The sample file is version 2; its records are still 32 bytes.

The first version kept only the exit before the last resume. It overshot wherever trips come faster than the bell's delay:
a ring in the resident between two short stretches is taken two or more trips later and was counted EL0. With 16
stretches, 75 of the io5 run's 10.8 M samples were rung before every stretch kept (counted resident, flagged); 89% of the
no-place samples are one stretch back, 99% within three.

Shares in % of the samples that were not halted, beside the guest bucket's % of busy time in the same run (bench cases
`guest-hackbench-sample`, `io5-solo32-sample`, `guest-scimark32-sse2-sample`, M2 Max, one run each; the a7cd8e82 column
is the table above, its EL0 share converted to non-halted samples):

| Case, phase | a7cd8e82: EL0 / bucket | exit only (a6f73186): EL0 + no place / bucket | 16 stretches (839f51ac): EL0, no place, sum / bucket |
| --- | ---: | ---: | ---: |
| `guest-hackbench-sample` run1 | 3.2 / 38.4 | 67.3 / 46.2 | 5.6, 39.9, **45.5** / 46.4 |
| `guest-hackbench-sample` run2 | | 68.6 / 47.0 | 6.6, 39.8, **46.4** / 47.3 |
| `io5-solo32-sample` configure | 23.5 / 37.0 | 46.4 / 40.1 | 28.2, 11.7, **39.9** / 40.1 |
| `io5-solo32-sample` i386-configure | 25.4 / 39.4 | 50.6 / 44.8 | 34.0, 11.9, **45.9** / 45.9 |
| `io5-solo32-sample` make | 60.3 / 68.1 | 78.7 / 73.4 | 66.6, 6.8, **73.4** / 70.2 |
| `io5-solo32-sample` i386-make | 65.6 / 71.9 | 81.4 / 76.6 | 73.7, 6.2, **79.9** / 76.9 |
| `guest-scimark32-sse2-sample` run1 | 92.6 / 95.0 | 99.8 / 98.5 | 99.8, 0.0, **99.8** / 98.6 |

The sum lands within a point of the bucket on hackbench and both configure steps, and 3 points above it on the make
steps; scimark32-sse2 does not move (0.01% no place). The excess on make is not explained here. Both time stamps count
their window toward EL0: the exit is stamped after the gate's register save (31 general and 32 vector registers) and
`finish`'s prologue, some 70 instructions after the exception, and the resume about 50 loads before the `eret`; a few
tens of nanoseconds per trip, at roughly 0.4 M trips per busy vCPU-second in a make step. But the guest bucket's own edges
(`perf::enter` before the resume call and in `returned`) are wider still, so those windows alone would put the samples
below the bucket, not above it.

The places of EL0 samples (sections 2-4) are unchanged in kind: the samples placed are the same ones as before, and the
no-place ones have no PC to give. What changes is that the placed samples are now a known share of EL0 time: about 70%
on configure, 12% on hackbench, 90% on the make steps.

The validity case passed (`guest-sampler-check-sample`, M2 Max). `guest-sampler-cost` (M2 Max) does not hold its bounds
reliably, with or without the fix. Guest share sampled minus unsampled, in points, `build` at 1 vCPU / at 4 vCPUs, run1
(the larger of the two runs each time; `hackbench` stayed within 0.1-0.9):

| Commit | Rounds (in order of running) | Verdict |
| --- | --- | --- |
| 839f51ac / c4321f9b (same resident code) | -2.02 / -2.20; -0.74 / -2.37; -1.93 / -1.62 | failed, failed, passed |
| 65a3d1eb (before the fix) | -1.90 / -1.95; -1.29 / -1.89 | passed, failed |

The two commits alternated over five runs: 839f51ac, 65a3d1eb, c4321f9b, 65a3d1eb, c4321f9b. The wall bound failed on both
commits (base: `hackbench` and `build` at 4 vCPUs in its second run). The guest-share difference sits at 1.6-2.4 points on
`build` at 4 vCPUs either way; the means differ by under 0.2 points (the fix's 0.14 further at 4 vCPUs, 0.04 nearer at 1),
inside the fix's own spread of 0.75. The fix adds a few
stores per trip in both modes and a walk of at most 16 pairs per sample (4,000 per second), which cannot account for even
that. The 2-point bound is marginal at 65a3d1eb already.

## 2. Where the vCPUs were

Samples by kind (% of samples), and EL0 samples by place (% of EL0 samples), 250 us requested, 3,950-3,995 samples per
second per vCPU measured, no sample lost. Dispatch is split by the call test (the target follows a call: a return; does
not: an indirect jump or call, or a checked link; unknown). Bench phases are run1 (run2 agrees within a point).

| Case, phase | EL0 | resident or host | halted | block | probe | slot: return | slot: not a call | slot: unknown | user | kernel |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `guest-build-sample` run1 | 47.7 | 52.3 | 0.0 | 89.1 | 0.0 | 6.7 | 3.8 | 0.4 | 95.0 | 5.0 |
| `guest-build-sample-4` run1 | 27.5 | 37.8 | 34.8 | 89.1 | 0.0 | 6.6 | 3.9 | 0.4 | 90.6 | 9.4 |
| `guest-hackbench-sample` run1 | 3.2 | 96.8 | 0.0 | 93.8 | 0.0 | 5.9 | 0.2 | 0.1 | 5.9 | 94.1 |
| `guest-luajit-sample` run1 | 67.3 | 32.7 | 0.0 | 95.3 | 0.0 | 2.5 | 2.1 | 0.1 | 98.0 | 2.0 |
| `guest-sqlite-sample` run1 | 53.1 | 46.9 | 0.0 | 89.0 | 0.0 | 5.5 | 5.5 | 0.0 | 99.0 | 1.0 |
| `guest-zstd-sample` run1 | 91.3 | 8.7 | 0.0 | 99.4 | 0.0 | 0.6 | 0.0 | 0.0 | 97.8 | 2.2 |
| `guest-startup32-sample` run1 | 52.5 | 47.5 | 0.0 | 88.8 | 0.0 | 7.1 | 3.5 | 0.6 | 98.3 | 1.7 |
| `guest-scimark32-sse2-sample` run1 | 92.6 | 7.4 | 0.0 | 97.1 | 0.0 | 2.9 | 0.0 | 0.0 | 100.0 | 0.0 |
| `io5-solo32-sample` configure | 6.2 | 20.2 | 73.6 | 89.0 | 0.0 | 6.4 | 3.2 | 1.4 | 53.7 | 46.3 |
| `io5-solo32-sample` make | 58.2 | 38.2 | 3.5 | 88.5 | 0.0 | 7.5 | 3.4 | 0.5 | 96.5 | 3.5 |
| `io5-solo32-sample` i386-configure | 6.7 | 19.7 | 73.6 | 87.4 | 0.0 | 7.4 | 3.6 | 1.6 | 62.8 | 37.2 |
| `io5-solo32-sample` i386-make | 63.8 | 33.5 | 2.7 | 87.8 | 0.0 | 8.5 | 3.3 | 0.3 | 96.9 | 3.1 |

Head and step places took no measurable share anywhere. The slot by privilege (a scratch reduction of the same
`.samples` files with the report's reader, run1, % of EL0): `build` user returns 6.39, user not-a-call 3.83, kernel
returns 0.30; `sqlite` user returns 5.45, user not-a-call 5.49; `hackbench` kernel returns 5.67 (of only 1,906 EL0
samples, about 8% of its EL0 time, section 1), user 0.26; `scimark32-sse2` user returns 2.92; `zstd` 0.57; `luajit` user
returns 2.37, not-a-call 2.09; `startup32` user returns 6.96, not-a-call 3.54.

## 3. Roles: FEX's body, its flag words, natemu's words

% of EL0 samples, raw / one-word skid correction (the truth between, section 1). "Hot 50" is FEX's body as % of the
samples on the phase's 50 busiest blocks (the plan's FEX-body row).

| Case, phase | FEX body | FEX flags | natemu | split + probe | exits | hot 50 FEX body |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `guest-build-sample` run1 | 66.8 / 42.4 | 5.4 / 11.4 | 16.8 / 35.2 | 2.3 / 4.1 | 8.2 / 0.7 | 74.7 / 50.6 |
| `guest-build-sample-4` run1 | 66.4 / 42.1 | 4.9 / 10.9 | 17.8 / 36.0 | 2.5 / 4.4 | 8.7 / 1.5 | 72.5 / 49.5 |
| `guest-hackbench-sample` run1 | 80.2 / 48.6 | 2.5 / 8.7 | 11.2 / 36.6 | 2.3 / 3.6 | 4.4 / 1.4 | 88.8 / 56.7 |
| `guest-luajit-sample` run1 | 72.5 / 57.9 | 3.5 / 14.8 | 19.4 / 22.7 | 4.4 / 7.0 | 12.9 / 0.1 | 74.4 / 62.9 |
| `guest-sqlite-sample` run1 | 68.1 / 46.9 | 3.6 / 11.3 | 17.3 / 30.8 | 5.0 / 6.0 | 9.4 / 0.5 | 73.6 / 61.6 |
| `guest-zstd-sample` run1 | 73.0 / 60.9 | 12.2 / 20.3 | 14.1 / 18.1 | 2.4 / 3.6 | 9.7 / 0.0 | 73.4 / 62.0 |
| `guest-startup32-sample` run1 | 68.2 / 39.1 | 3.7 / 11.2 | 16.9 / 38.5 | 3.6 / 6.6 | 8.8 / 0.9 | 72.6 / 49.4 |
| `guest-scimark32-sse2-sample` run1 | 78.0 / 48.0 | 2.0 / 6.5 | 17.1 / 42.6 | 10.4 / 17.7 | 0.2 / 0.1 | 80.4 / 49.4 |
| `io5-solo32-sample` make | 64.4 / 41.0 | 3.9 / 10.8 | 20.2 / 36.7 | 2.5 / 4.1 | 11.1 / 1.6 | 69.0 / 48.1 |
| `io5-solo32-sample` i386-make | 63.2 / 36.6 | 4.2 / 9.6 | 20.3 / 41.5 | 2.3 / 4.7 | 9.3 / 1.3 | 67.3 / 39.9 |

FEX's body is mostly memory words: raw `fex_mem` is 29-46% of EL0 (build 39.0, sqlite 43.3, startup32 46.4), and the
correction moves nearly all of it onto the next word (4-7% left), so the raw figure is skid on the loads themselves.
natemu's words, raw, by kind (% of EL0): linked exits (`exit_literal`) 8.1 on build, 9.2-11.0 on the make steps; stack
checks 2.9 build, 3.4 make, 5.1 i386-make; split checks 2.3 build, 10.4 scimark32-sse2; block entry words 2.5 build,
2.1-2.8 make; load-then-move 0.3-0.6; guard checks 0.3; x87 run tests 1.4 on scimark32-sse2, nothing elsewhere. The
exits' share almost vanishes under the correction (8.2 to 0.7 on build): those samples are skid from the word
before, or the next block's first word.

## 4. By symbol and string code

% of EL0 samples, run1. REP string bodies count samples on a REP MOVS or STOS instruction's words; libc wide is the AVX2
proxy (no glibc AVX2 function is named; `avx2named` is 0.0-0.1 everywhere).

| Case | top symbols | REP string | libc wide | kernel copy/clear | flag words (raw / corrected) |
| --- | --- | ---: | ---: | ---: | ---: |
| `guest-build-sample` | `cc1:?` 31.8, `as:?` 7.1, `libc.so.6:?` 3.1, `cc1:ira_setup_alts` 1.7, `cc1:bitmap_set_bit` 1.4 | 3.1 | 1.1 | 1.1 | 5.4 / 11.4 |
| `guest-hackbench-sample` | `arch_exit_to_user_mode_prepare` 9.7, `clear_page_rep` 9.6, `ktime_get_coarse_real_ts64` 5.1, `rep_movs_alternative` 4.9, `entry_SYSCALL_64` 4.1 | 12.4 | 0.0 | 14.7 | 2.5 / 8.7 |
| `guest-luajit-sample` | `user:?` 57.4 (code luajit generated), `luajit:?` 32.9 | 0.8 | 0.0 | 0.8 | 3.5 / 14.8 |
| `guest-sqlite-sample` | `speedtest1:?` 83.0, `ambiguous:locks|speedtest1` 3.3 | 0.4 | 0.0 | 0.5 | 3.6 / 11.3 |
| `guest-zstd-sample` | `ZSTD_RowFindBestMatch_noDict_5_6` 70.6, `ZSTD_compressBlock_lazy2_row` 5.8, `busybox:?` 4.4, `ZSTD_row_update` 3.1 | 6.7 | 0.0 | 1.3 | 13.8 / 22.8 (compress) |
| `guest-startup32-sample` | `speedtest1-32:?` 75.5, `user:short` 11.7 | 7.9 | 0.0 | 0.2 | 3.7 / 11.2 |
| `guest-scimark32-sse2-sample` | `SOR_execute` 28.8, `SparseCompRow_matmult` 19.9, `Random_nextDouble` 14.7, `FFT_transform_internal` 13.1, `LU_factor` 10.5 | 0.0 | 0.0 | 0.0 | 2.0 / 6.5 |
| `io5-solo32-sample` make / i386-make | `user:?` 85.3 / 84.9 (block level), `clear_page_rep` 0.4 / 0.2 | 1.0 / 4.5 | | 0.5 / 0.3 | 3.9 / 4.2 |

zstd's whole-run REP share comes from its short verify phase (51-55% REP MOVS in busybox there). startup32's is REP MOVS
(7.7). In every boot phase the kernel's `memcpy_orig`, `clear_page_rep` and the crypto self-tests dominate (13-14% kernel
copy and clear).

## 5. The census (QEMU)

Run 1 of each benchmark, `guest-<b>-census`, M2 Max, run 20261123-183251-bd87. Indirect jmp and call executions to each
site's main target (top-1) and to its top two, execution-weighted, per address space and by code (sites merged by
physical code, targets relative to the site); lower bounds by at most the overflow (executions beyond the 4 targets
kept). Returns per 1,000 guest instructions and the return model's hit rate (one stack per vCPU; user and kernel apart).

| Benchmark, mode | indirect /1k | top-1 / top-2 per space | top-1 / top-2 by code | overflow | ret /1k | depth 8 / 16 / 32, one stack | depth 16, split |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| build, user | 4.88 | 63.0 / 73.4 | 57.1 / 67.9 | 21.2 | 15.6 | 99.2 / 99.9 / 100.0 | 99.9 |
| build, kernel | 0.38 | 87.3 / 93.0 | 87.3 / 93.0 | 5.7 | 25.7 | 97.8 / 99.9 / 99.9 | 99.9 |
| hackbench, user | 0.02 | 97.3 / 99.0 | 96.6 / 99.0 | 0.7 | 61.5 | 0.0 / 99.7 / 99.8 | 99.9 |
| hackbench, kernel | 0.97 | 100.0 / 100.0 | 100.0 / 100.0 | 0.0 | 27.1 | 99.8 / 100.0 / 100.0 | 100.0 |
| luajit, user | 6.22 | 75.4 / 78.5 | 75.3 / 78.4 | 21.5 | 7.6 | 99.9 / 100.0 / 100.0 | 100.0 |
| sqlite, user | 15.32 | 54.8 / 64.3 | 54.8 / 64.3 | 35.0 | 15.1 | 99.8 / 100.0 / 100.0 | 100.0 |
| sqlite, kernel | 0.59 | 95.8 / 98.0 | 95.8 / 98.0 | 0.5 | 24.5 | 97.0 / 99.9 / 99.9 | 99.9 |
| startup32, user | 11.25 | 54.7 / 63.7 | 54.7 / 63.7 | 35.3 | 23.3 | 99.7 / 100.0 / 100.0 | 100.0 |
| zstd, user | 0.11 | 99.7 / 99.9 | 99.6 / 99.9 | 0.0 | 1.3 | 99.8 / 99.9 / 100.0 | 100.0 |
| scimark32-sse2, user | 0.00 | 97.5 / 98.9 | 97.4 / 98.8 | 0.6 | 2.0 | 100.0 / 100.0 / 100.0 | 100.0 |

hackbench's user depth-8 figure is as the reducer reports it (not looked into); with user and kernel apart it is 99.9%. Every other kernel row hits 97-99% at depth 8 and 99.7% or more at 16.

## 6. One resident trip (D3)

`guest-coldtrip-profile`, two runs, 3 counted rounds each (12 rounds), guest-bucket ns per CPUID trip: the gate's fixed
cost median 57.7 ns (54.2-63.4, stdev 2.9); the cold state at a few hundred KiB of code median 33.2 ns (21.6-43.0, stdev
7.0); pooled per run 32.8 and 32.7 ns. The whole trip (every bucket but halted) is 378-384 ns small and 404-412 ns large
(medians). A CPUID trip touches less resident state than a fault, a system call or a miss: the cold state is a lower bound.

Against each step's guest bucket in `io5-solo32-profile` (unperturbed, same machine; step times 74.8, 111.6, 82.5, 139.7 s):

| Step | guest bucket (vCPU-s) | trips (M, estimate) | gate % | cold state % | both % |
| --- | ---: | ---: | ---: | ---: | ---: |
| configure | 30.35 | 65.2 | 12.4 (11.6-13.6) | 7.1 (4.6-9.2) | 19.5 (16.3-22.9) |
| make | 299.74 | 187.9 | 3.6 (3.4-4.0) | 2.1 (1.4-2.7) | 5.7 (4.7-6.7) |
| i386-configure | 35.91 | 62.8 | 10.1 (9.5-11.1) | 5.8 (3.8-7.5) | 15.9 (13.2-18.6) |
| i386-make | 395.82 | 202.7 | 3.0 (2.8-3.2) | 1.7 (1.1-2.2) | 4.7 (3.9-5.5) |

Ranges: the rounds' minimum and maximum. Over the whole use launch (exact count) the trips' gate and cold state are 6.4%
of 860.9 vCPU-s of guest bucket. Configure's trips are mostly dispatcher misses (29.0 M), helpers (14.1 M), links (11.8 M)
and aborts (10.2 M).

## 7. Decisions

| Rule (plan) | The number | Case(s), M2 Max, a7cd8e82 | Verdict |
| --- | --- | --- | --- |
| Inline cache per site (sampler plan section 7; indirect plan step 2): slot and landing words >= 8% of EL0 on `build` or a make step, and main target >= 60% per address space and by code | slot alone 10.9% (`build` run1), 11.4% make, 12.2% i386-make (landing words not separated by the report); `build` user top-1 63.0% per space but 57.1% by code; make steps: no census | `guest-build-sample`, `io5-solo32-sample`; `guest-build-census` | **don't**, for now: by code `build` is under 60%; step 2 is deferred, and `io5-solo32-census` runs only if it comes up |
| Indirect step 1, a branch per site (indirect plan section 10): not-a-call slot samples >= 3% of EL0 on a profiled benchmark, and the host kill-test passes | `sqlite` 5.49%, `build` 3.83%, `startup32` 3.54% (user, run1); `luajit` 2.09; make 3.4, i386-make 3.3 | `guest-{sqlite,build,startup32,luajit}-sample`, `io5-solo32-sample` | **build**, if the host kill-test passes (not run) |
| Return prediction (sampler plan section 7): returns >= 5% of EL0, depth-16 model >= 90% | returns 6.7% `build`, 7.5% make, 8.5% i386-make, 5.5% `sqlite`, 7.1% `startup32`; depth 16: 99.7-100% | the sample cases above; `guest-*-census` | **design it**: done, the [return-prediction plan](return-prediction-plan.md) |
| Return prediction, smallest version (return plan section 11): return slot samples plus landing words >= 3% of EL0 on covered absolute code | `sqlite` user 5.45%; `hackbench` kernel 5.67% (1,906 EL0 samples, about 8% of its EL0 time: weak); `scimark32-sse2` 2.92%; `zstd` 0.57% (landing words not counted: these are lower bounds) | `guest-{sqlite,hackbench,scimark32-sse2,zstd}-sample` | **build**, after the indirect step 1 and if the host kill-test passes: `sqlite` clears 3% on the slot alone. Built, then removed on November 24: a measured slowdown ([return plan](return-prediction-plan.md) sections 15 and 16) |
| FEX's body >= 60% of the hot blocks | hot 50, raw / corrected: `build` 74.7 / 50.6, make 69.0 / 48.1, i386-make 67.3 / 39.9, `sqlite` 73.6 / 61.6, `luajit` 74.4 / 62.9, `zstd` 73.4 / 62.0 (its compress phase 71.6 / 58.7), `scimark32-sse2` 80.4 / 49.4 | sample cases above | **undecided** on `build` and the make steps (60 lies between the bounds); met on both bounds for `sqlite`, `luajit` and `zstd` (run1): the FEX code-quality track goes to the owner on those, with their guest bucket against the real-x86 reference beside it |
| Split checks and probes >= 3% | `scimark32-sse2` 10.4 / 17.7, `sqlite` 5.0 / 6.0, `luajit` 4.4 / 7.0, `startup32` 3.6 / 6.6; `build` 2.3 / 4.1, make 2.5 / 4.1 | sample cases above | **build** (look at which stores keep their own check), on `scimark32-sse2` above all |
| REP string bodies >= 3% | `hackbench` 12.4 (kernel `clear_page_rep`, `rep_movs_alternative`), `startup32` 7.9 (MOVS), `zstd` 6.7 (its verify phase), i386-make 4.5, `build` 3.1, make 1.0 | sample cases above | **build** (bulk `rep movs` inside a page), `startup32` and `hackbench` first; `build` barely |
| glibc AVX2 string functions >= 3% | named 0.0-0.1; libc wide-vector blocks 1.1 on `build`, 0 elsewhere | sample cases above | **don't**: recorded (the proxy may undercount: libc is stripped) |
| Flag words >= 3% | raw / corrected: `zstd` compress 13.8 / 22.8, `build` 5.4 / 11.4, `sqlite` 3.6 / 11.3, make 3.9 / 10.8, `luajit` 3.5 / 14.8 | sample cases above | **build** (flag liveness across extended units), `zstd` first |
| Trip cost times trips >= 10% of configure's guest bucket | gate plus cold state 19.5% (16.3-22.9) of configure, 15.9% of i386-configure; cold state alone 7.1% (4.6-9.2) | `guest-coldtrip-profile` x2, `io5-solo32-profile` | **build** (the resident's return path and footprint): the gate's fixed cost is the larger part; the trips per step are an estimate (section 1) |
| Anything under 3% | stack checks 2.9 `build` (5.1 i386-make), load-then-move 0.3-0.6, x87 run tests 0-1.4, guard checks 0.3 | sample cases above | recorded, left (stack checks on i386-make to look at with the split checks) |

What the decisions rest on, and what would move them: configure's and hackbench's EL0 tables describe a minority of
their EL0 time (section 1), so no verdict reads them; a sampler that places those samples (section 1's fix) would let the
configure steps be read. The FEX-body row needs a tighter skid model before `build` can be decided.

## Owner decisions (November 23)

- Build every fix whose row reads "Build": indirect step 1, return prediction, the resident's trip cost (return path
  and footprint), bulk REP string operations, flag words, and page-crossing checks. Correctness first. (Return prediction was built and then removed on November 24, at the owner's
  request: it made returns slower, [return plan](return-prediction-plan.md) section 15. Indirect step 1 was built and then
  reverted the same day: it made `guest-luajit-interp` 7-12% slower, [indirect plan](indirect-cache-plan.md) section 14.)
- Pool them: no A/B per fix; one combined A/B against this page's commit and one profile round over all of them at the
  end, with the gate. Each fix keeps its own kill-test and correctness tests.
- No FEX code-quality track for now.

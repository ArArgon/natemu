# CPU and RAM capacity

Status: November 7, 2026. The targeted capacity checks below pass. The full
regression suite is accounted for through the corrected reruns below. The planned
186 benchmark runs and 60 focused follow-up boots completed with valid outputs
and independent review. The capacity increment is complete within the scope below;
a small installer boot cost remains an explicit performance follow-up.

## Implemented limits

The [ABI](../../crates/abi/src/execution.rs) now derives the design limit of
**64 vCPUs** from its 64-bit CPU masks. Admission also checks the host's advertised
HVF vCPU limit. [VM creation](../../crates/host/src/vm.rs) checks the required IPA
width against HVF's available range, so 64 vCPUs is not a promise for every host.

The current private mapping/table layout derives a maximum of **34 GiB installed
guest RAM**, with a physical span ending at 35 GiB. RAM above 3 GiB resumes at
physical address 4 GiB; the 3–4 GiB device hole remains reserved. The legacy
VGA/ROM hole also remains excluded. The [Linux E820 map](../../crates/host/src/boot.rs)
and allocator use this split layout. These are current layout bounds, not an
x86 architectural RAM limit.

The [split-access protocol](../../crates/runtime/src/cache.rs) now sends shadow
reset requests to all relevant peers before waiting for their acknowledgements.
The split-access lock still serializes the atomic operation. This implements the
previous batching backlog item. Earlier and later stress timings were not paired
benchmark measurements and do not support a precise speedup claim.

## Targeted results

[Fourteen internal checks](../../../natemu-raw/capacity-20261107-01/live-batched-01/results.json)
passed: each row below ran with both the default block size and one instruction
per block. The [runner](../../tools/run_capacity.py) retains commands and errors.

| Configuration | Checked behavior |
| --- | --- |
| 64 CPUs, 2 GiB | All CPUs observe modified code; exact cross-page atomic count |
| 64 CPUs, 2 GiB | Every CPU reports the expected UD2 fault |
| 64 CPUs, 34 GiB, allocation near the physical end | Every CPU fetches through the high-GPA mapping and reports the expected UD2 fault |
| 4 CPUs, 4 GiB, allocation crossing 3 GiB | Allocation skips the device hole; code coherence and split atomics pass |
| 64 CPUs, 8 GiB, allocation above 4 GiB | Code coherence and split atomics pass |
| 1 CPU, 34 GiB, allocation near the physical end | Code coherence and split atomics pass |
| 1 CPU, 34 GiB, last physical page | Executes code in the last page, writes the final byte, and checks a nearby cross-page atomic |

The combined 64-CPU/34-GiB case is a high-address fetch/fault check. These sparse
fixtures do **not** establish that all 34 GiB can be resident simultaneously.

Six [APIC checks](../../tools/run_capacity_apic.py) passed at 64 CPUs, again with
both block sizes: physical x2APIC delivery across CPU-index boundaries, all/self-
excluding broadcasts, and interrupt delivery with observed HLT activity. Each
uses exact per-CPU delivery counts. HLT activity does not prove every target was
parked at delivery. The retained [archive](../../../natemu-raw/capacity-20261107-01/apic-01.tar.gz)
and all 25 file members were independently checked against its
[hash manifest](../../../natemu-raw/capacity-20261107-01/apic-01.tar.manifest.json).
This is an internal architectural oracle, without a native/QEMU comparison.

## Linux resident-memory check

Both [Linux runs](../../../natemu-raw/capacity-20261107-01/linux-02/capacity-linux.json)
passed using the unchanged Debian 6.12.107 kernel and existing benchmark initrd,
with 8 GiB configured RAM. The [runner](../../tools/run_capacity_linux.py) retains
full serial output, input hashes, the command, and parsed observations.

| CPUs | Linux online mask | MemTotal (KiB) | Stress worker RssAnon (KiB) |
| --- | --- | ---: | ---: |
| 1 | `0` | 8,123,628 | 4,194,572 |
| 8 | `0-7` | 8,121,780 | 4,194,576 |

Unmodified stress-ng 0.19.02 used one worker, `--vm-bytes 4294967296`,
`--vm-method one-zero`, `--vm-ops 1`, `--vm-keep`, and `--verify`. Source inspection
confirms that this method writes and verifies every 64-bit word across the whole
4-GiB buffer with all-one and all-zero patterns. Both runs completed the expected
262,144 reported bogo operations with no verification failure. Resident anonymous
memory exceeded 4 GiB; guest SwapTotal and worker VmSwap were zero.

This verifies more than a virtual reservation. It does not identify the worker's
physical page numbers, prove absence of every possible address-alias defect, or
rule out host swapping. The earlier `linux-01` attempt is retained as an
unaccepted harness result: the online-CPU marker was not parsed correctly; both
configurations were rerun in `linux-02`.

## Independent checks

A separate reviewer rebuilt the frozen source into an independent target directory.
The boot executable, driver and resident hashes match the candidate binaries. The
resident stack/ERET audit passes: 18,512 bytes for the gate path, 22,704 including
the nested halt path, and 8,944 for boot; the dispatcher uses no stack.

The two normally ignored host tests were also executed with retained inputs:
14 admission objects passed, and ten recorded requests matched pipe versus
shared-memory transport in each of default, `--report`, and fork server modes.
Both transports used the same frozen FEX. These checks do not establish historical
compiler equivalence or a timing result. See [supplemental results](../../../natemu-raw/capacity-20261107-01/independent/ignored-results.json).
Independent VM replication passed six capacity cells (64-vCPU code coherence and
split atomics, combined 64-vCPU/34-GiB fetch/fault, and the last physical page,
each at both block sizes) and both Linux resident-memory configurations. See
[replication results](../../../natemu-raw/capacity-20261107-01/independent/replication-complete.json).

## Full-suite result and retained attempts

All 136 planned suite groups are accounted for in the independent
[acceptance record](../../../natemu-raw/capacity-20261107-01/independent/suite-gate.json):
133 original passing rows plus the corrected limit check and two real ASM runs.
The seven known QEMU two-NaN departures remain qualified at both block sizes; the
350 Python tests pass without skips. Four additional synthetic controls verify
that missing worker results, failed result writes, and failed status printing
cannot produce a false suite success.

In attempt 04, the old `limits` oracle
incorrectly required five CPUs to be rejected; it was changed to reject 65 CPUs,
and all five cases pass in the reviewed, lock-coordinated
[limits supplement](../../../natemu-raw/capacity-20261107-01/limits-02/limits.json).
The ASM rows initially discovered zero tests because their public input directory
was absent from the source snapshot. Those rows are invalid for coverage. The
local runner now rejects an empty selection; the unchanged 2,270-test public set
ran completely at both block sizes: each produced 2,088 passes, 48 declared x87
precision deviations, and 134 explicit unsupported results, with no unexpected
failures. Public sources were unchanged and hash-checked before and after. The
remaining large-region fixture exercises excluded 32-bit address wrapping. The
local runner now describes its configured-RAM bound honestly. Neither issue
required a runtime change.

The earlier attempts remain visible. Attempt 01 stopped for harness repair;
`suite-02` exhausted disk space, including explicit ENOSPC tracebacks in its
fault-test logs. `suite-03` lost its supervisor, leaving only partial results.
These attempts do not count as a completed suite and their infrastructure failures
are not CPU-semantic failure evidence. See the retained
[attempt status](../../../natemu-raw/capacity-20261107-01/suite-attempt-01-status.json),
[suite-02 rows](../../../natemu-raw/capacity-20261107-01/suite-02/results.txt), and
[suite-03 rows](../../../natemu-raw/capacity-20261107-01/suite-03/results.txt).

## Performance results

All [186 planned boots](../../../natemu-raw/capacity-20261107-01/bench/summary.json)
completed and verified, with no missing, failed, capped or contended runs. The
[independent audit](../../../natemu-raw/capacity-20261107-01/independent/performance-review.json)
checks exact commands, input hashes, raw serial markers and timing calculations.

The M4 host has four performance and six efficiency cores, with 16 GiB RAM.
The baseline is the preceding correctness build, and the candidate includes the
capacity changes and batched split-lock requests. Both use the same frozen FEX,
Linux kernel and unchanged benchmark initrd, with compile caching off and
`mitigations=off nokaslr`. One VM runs at a time. Each workload boot runs cold then
warm; installer boot is timed only to its language-selection endpoint.

At 1 and 4 vCPUs, RAM stays at 2 GiB. Three before/after pairs alternate order.
The tables report absolute seconds, lower being faster. Before and after are
separate medians of three runs; the detailed analysis also retains all paired
differences. Serial reads are polled at 50 ms, with actual read brackets retained.
Those brackets describe observer uncertainty, not statistical confidence intervals.

### 1 vCPU — cold seconds

| Benchmark | Before | After | QEMU reference | Native AMD reference |
| --- | ---: | ---: | ---: | ---: |
| build | 11.438 | 11.465 | 29.34 | 2.61 |
| zstd | 3.007 | 3.004 | 29.78 | 2.12 |
| hackbench | 11.847 | 11.944 | 16.34 | 3.87 |
| locks | 3.795 | 3.804 | 11.08 | 2.12 |
| luajit | 4.074 | 4.056 | 18.38 | 1.80 |
| luajit-interp | 4.083 | 4.061 | 30.34 | 2.29 |
| mem | 7.876 | 7.911 | 16.64 | 4.69 |
| cray | 3.152 | 3.157 | 130.35 | 3.66 |
| 7zip | 3.867 | 3.943 | 20.80 | 3.03 |
| sqlite | 7.308 | 7.308 | 22.82 | 2.38 |
| avx | 8.682 | 8.675 | 45.12 | 2.90 |
| installer | 5.495 | 5.598 | 6.33 | — |

### 4 vCPUs — cold seconds

| Benchmark | Before | After | QEMU reference | Native AMD reference |
| --- | ---: | ---: | ---: | ---: |
| build | 5.444 | 5.419 | 12.35 | 1.09 |
| zstd | 1.483 | 1.529 | 11.09 | 0.97 |
| hackbench | 3.533 | 3.346 | 5.39 | 1.72 |
| locks | 7.623 | 7.340 | 27.55 | 4.21 |
| luajit | 1.467 | 1.492 | 5.32 | 0.60 |
| luajit-interp | 1.199 | 1.247 | 9.05 | 0.64 |
| mem | 2.662 | 2.681 | 4.99 | 1.44 |
| cray | 1.163 | 1.160 | 42.14 | 1.08 |
| 7zip | 1.756 | 1.760 | 6.44 | 1.29 |
| sqlite | 7.328 | 7.338 | 22.76 | 2.45 |
| avx | 3.556 | 3.570 | 16.56 | 1.12 |
| installer | 4.426 | 4.519 | 4.89 | — |

QEMU and native AMD columns reuse the [November 5 references](perf-phase2-plan.md#phase-result-november-5-244f270-natemu-rawbatch2-final-20261105-01); they were not rerun in this campaign. QEMU is the retained like-for-like CPU profile on the M4; native AMD is the earlier Linux reference VM on another host, not a same-hardware speedup baseline or a fresh measurement from `192.168.12.11`.

The original screen found no large repeatable slowdown, but it did not establish
equivalence: the one-second N4 workloads had broad read brackets, and installer
N4 had a small positive signal in all three pairs even at the lower read bounds.
The completed follow-up below resolves the short-workload ambiguity and retains
the installer cost explicitly.

### Candidate elapsed times

Seconds, median of three runs; each cell is cold / warm. Installer has only its
boot endpoint. N1/N4 use 2 GiB; N8/N10 use 8 GiB. Changing both CPU count and RAM
means these are configuration comparisons, **not isolated CPU-scaling results**.


| Benchmark | 1 CPU | 4 CPUs | 8 CPUs | 10 CPUs |
| --- | ---: | ---: | ---: | ---: |
| build | 11.465 / 10.060 | 5.419 / 3.827 | 5.685 / 3.504 | 6.525 / 3.613 |
| zstd | 3.004 / 2.826 | 1.529 / 1.263 | 1.420 / 1.167 | — |
| hackbench | 11.944 / 11.742 | 3.346 / 3.545 | 3.266 / 3.267 | — |
| locks | 3.804 / 3.738 | 7.340 / 7.150 | 10.993 / 10.807 | 12.861 / 12.720 |
| luajit | 4.056 / 3.905 | 1.492 / 1.319 | 1.397 / 1.174 | — |
| luajit-interp | 4.061 / 3.953 | 1.247 / 1.090 | 1.035 / 0.848 | — |
| mem | 7.911 / 7.674 | 2.681 / 2.397 | 2.559 / 2.297 | 2.306 / 1.692 |
| cray | 3.157 / 3.076 | 1.160 / 1.067 | 0.964 / 0.859 | — |
| 7zip | 3.943 / 3.692 | 1.760 / 1.540 | 1.621 / 1.336 | — |
| sqlite | 7.308 / 7.052 | 7.338 / 7.078 | 7.400 / 7.212 | — |
| avx | 8.675 / 8.436 | 3.570 / 3.325 | 2.824 / 2.492 | — |
| installer | 5.598 | 4.519 | — | — |

N10 intentionally covers build, memory and locks only; the x264 output oracle is
available through N8. Every planned configuration has all three results.
More CPUs help several compute workloads but do not make every workload faster.
The lock test deliberately contends on one mutex and one atomic counter; its
larger-N cost is a profiling candidate, not proof of a capacity-change regression.
The workload's total iteration count is fixed. The memory workload keeps fixed
operation counts but increases its aggregate worker-memory footprint with N.

Full per-pair times and observer brackets are in the
[analysis](../../../natemu-raw/capacity-20261107-01/bench-analysis.json).

## Focused timing follow-up

All 60 additional boots completed and verified. Six balanced before/after pairs
per selected configuration used 5-ms polling, with exactly the same binary and
image hashes as the original campaign. Public tests were unchanged. Selection
was post-hoc and observer overhead changed, so these results are separate from
the original 186; they are not pooled. The [independent review](../../../natemu-raw/capacity-20261107-01/independent/performance-followup-review.json)
verified commands, raw outputs, hashes and every timing calculation.

Seconds; before/after are separate medians, and the last column is the median
within-pair difference. A positive difference means slower.

| Case | Before | After | Paired difference |
| --- | ---: | ---: | ---: |
| luajit-interp N4 cold | 1.2600 | 1.2485 | -0.0040 |
| luajit-interp N4 warm | 1.1045 | 1.1085 | +0.0040 |
| zstd N4 cold | 1.5150 | 1.5205 | -0.0010 |
| zstd N4 warm | 1.3225 | 1.3080 | -0.0055 |
| cray N4 cold | 1.1835 | 1.1800 | -0.0010 |
| cray N4 warm | 1.0920 | 1.0965 | +0.0055 |
| installer N1 cold | 5.5155 | 5.5305 | +0.0235 |
| installer N4 cold | 4.5175 | 4.5750 | +0.0575 |

The short-workload paired medians are within 0.0055 seconds of zero and include
both faster and slower pairs; their original apparent slowdowns were not
reproduced. Installer N4 retains an **unresolved small boot cost**: median paired
increase 0.0575 seconds, five of six pairs positive beyond the read bounds, with
both ordering-group medians positive. Individual changes range from −0.026 to +0.097
seconds. Installer N1 ranges from −0.043 to +0.147 seconds, with a paired median
increase of 0.0235 seconds. These observations do not prove zero performance
impact or identify its cause. Profiling the N4 boot cost is a backlog item; the
capacity increment has no stricter performance threshold from the owner.

## Historical main-branch reference

The checked local `main` and `origin/main` point to `fb1b24a` (same tree as
`5a0d7ba`). Their latest recorded full matrix is the November 5 performance-phase
run: the [original provenance](../../../natemu-raw/batch2-final-20261105-01/README.md)
names measured build `82462c2` before the authorship rewrite. It precedes the
final main tip and is **not an exact-tip measurement**. The table below is a
historical reference, not an interleaved main-versus-current regression test.

Cold seconds; current values are the original three-run candidate medians above.

| Benchmark | Main recorded N1 | Current N1 | Main recorded N4 | Current N4 |
| --- | ---: | ---: | ---: | ---: |
| build | 11.48 | 11.465 | 5.44 | 5.419 |
| zstd | 3.07 | 3.004 | 1.49 | 1.529 |
| hackbench | 11.86 | 11.944 | 3.69 | 3.346 |
| locks | 3.90 | 3.804 | 7.20 | 7.340 |
| luajit | 4.10 | 4.056 | 1.48 | 1.492 |
| luajit-interp | 4.19 | 4.061 | 1.29 | 1.247 |
| mem | 7.93 | 7.911 | 2.64 | 2.681 |
| cray | 3.26 | 3.157 | 1.12 | 1.160 |
| 7zip | 4.00 | 3.943 | 1.84 | 1.760 |
| sqlite | 7.39 | 7.308 | 7.47 | 7.338 |
| avx | 8.87 | 8.675 | 3.55 | 3.570 |
| installer | 5.42 | 5.598 | 4.48 | 4.519 |

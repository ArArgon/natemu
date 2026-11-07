# COR-02/03/05/06 instruction-form repairs

November 7, 2026. Implementation, independent review, full-suite, public-workload and performance
validation are complete.
This report concerns x64 CPU execution and ordinary RAM. Compatibility execution, call gates and x87 remain outside
this increment. Existing worktrees and public test sources are unchanged.

| Item | Implemented behavior | Cost is paid by |
| --- | --- | --- |
| COR-02 | VCVTPS2PH memory destinations preflight the whole store, convert all lanes with x86 rounding/flags, and publish only the permitted effects. Type 11 does not raise #AC. | These conversion stores use a resident helper; register forms keep their existing path. |
| COR-03 | ENTER supports nesting 0–31 and 16/64-bit operands, ordered frame construction and the final one-byte stack permission probe. Active REX.W overrides 66 for ENTER and LEAVE. | ENTER uses a resident helper, including nesting zero because its previous implementation omitted the required final probe. |
| COR-05 | Register-index BT/BTS/BTR/BTC adds the signed bit-string displacement before wrapping the effective address to 32 bits, then adds FS/GS. Full operand width and LOCK handling are preserved. | The affected address32 forms; no general memory-access guard was added. |
| COR-06 | Near CALL/JMP/RET follow the selected Intel forced-64-bit rule despite 66. Far CALL/JMP/RET and IRET support 16-bit operands with correct frame widths and preserved upper IRET flags. | Changed branch decoding and the affected far-transfer helpers. |

Review also corrected atomicity in the resident stack/frame helpers: naturally aligned slots use scalar accesses
of their architectural width. The shared far-frame path checks the whole frame before transferring its slots;
far pointers use their specified alignment rather than the total pointer length.

## Contracts and remaining evidence limits

The selected near-transfer contract follows Intel SDM Vol.1 §6.4.7. ENTER's final permission probe is one byte
(Vol.3C §31.4.4); it is not a store or a data-watchpoint access. Far-pointer alignment follows Vol.3A Table 7-7.
The integer half converter follows VCVTPS2PH's Type 11 exceptions and Vol.1 §4.9.1.5's tininess after rounding
to the destination precision with an unbounded exponent. See retained independent reviews for source excerpts.

Three limits remain explicit:

- ENTER's earlier frame stores may survive a later fault while RSP/RBP remain unchanged. This retains the
  AMD-observed partial-effect policy; Intel-specific write-back-memory prefix survival has not been established.
- Sixteen conversion test definitions deliberately check the selected exception policy: overflow plus precision
  for an unmasked overflow, and memory-first ordering when a destination fault competes with #XM. These establish
  implementation consistency, not universal hardware equivalence. The existing register-form exception policy is retained.
- The focused stop interface does not expose fault-time CS/SS. Normal far-transfer selector readback is checked;
  IRET16 RF preservation also lacks a direct end-to-end observation because a following instruction consumes RF.

Native reference access was attempted and was unreachable. QEMU's differing near-prefix/ENTER rules are not
substituted for the selected Intel contract.

## Validation and artifacts

Evidence root: `../natemu-raw/correctness-forms-20261107-02/`. `before/` preserves the initial root source state,
`suite-source/` the full-suite source snapshot, and `candidate-bin/` the binaries used for public/performance runs.
FEX commit `1df70f05044e3bf5a51652c79e5e6310eddaf6fe` is retained locally and in `fex-forms.bundle`; no push was made.

The final focused matrix has 516 programs and 1,960 CPL/block-size judgments: near transfers, ENTER/LEAVE,
far transfers, address32 bit operations and conversion stores, including faults, rounding and watchpoints.
The final 1,960 judgments passed completely (980 at each block size), as did the earlier 1,852-judgment development
snapshot. The complete standing suite also runs both unchanged public FEX ASM configurations and all other standing steps.
All 136 planned suite steps have accepted results. Independent rebuilding produced byte-identical host, runtime and
compiler binaries; independent replay passed 968 judgments across 265 programs (1,936 prefix/probe VM executions),
with no failures, missing outputs or caps. General Python tests passed 350/350.

Initial full-suite Cargo commands exposed a stale ABI test rejecting IRET16. After correcting that expectation,
both complete Cargo commands passed using an isolated build directory; original failures and reruns are retained.
Older project tests expecting BT/VCVTPS2PH refusals now check the supported behavior. No public test was patched.
The normally ignored admission fixtures and current-compiler pipe/channel replay also pass, including default,
report and fork-server modes. An attempted replay with the driver's `--max-block` option was an invalid server
invocation; its log is retained separately and the valid fork-server run completed.

One first xAPIC N4/default stress attempt hit the watchdog after one unreceived IPI; the permitted isolated rerun
passed all six stress cells. This remains an unexplained intermittent observation. Earlier retained suites passed
that step initially, so neither a pre-existing cause, noise, nor a regression attributable to these fixes is established.
The negative derived timer-expiry counts in the failed run use an unwritten end timestamp; they are not a second defect.
Both original output and rerun are preserved under `suite/`.

Other retained limits: i8b has exactly its seven known QEMU two-NaN differences at each block size. The i7 mutation
model cannot parse two raw-byte prefix programs; their original executions pass, but the generated page mutations
remain untested. The known QEMU null-SS IRET reference timeout does not skip natemu's model-based checks.

Unchanged public FEX ASM at each block size: **2,270 outcomes = 2,088 pass, 48 reduced-x87 precision deviations,
134 unsupported, zero unexpected failures**. This is eleven additional passes versus the previous build. Category
changes and source-inferred case identities are retained in `suite-evidence-audit.json`; the direct evidence for
that gain is the complete summary, since the standing runner removes passing per-case temporary files.

Public Linux selftests completed all 28 boots: xstate, ldt_gdt, nx_stack, sigaltstack, sigreturn, sysret_rip and
sysret_ss_attrs, at N1/N4 on both baseline and candidate. All fourteen before/after pairs match, including output
checksums. Each variant has ten successful executions and four unchanged rc139 compatibility outcomes (sigreturn
and sysret_ss_attrs at both CPU counts), not fourteen passes. The N1 LDT affinity skip runs successfully at N4.
No boot stalled, capped, lost output or overlapped another VM. Current buffered failure logs do not freshly localize
the compatibility faults; the previous console evidence and this before/after comparison have different roles.

All executed frozen hashes stayed unchanged. The suite's final boot-smoke step rebuilt the mutable target runtime
after the test-only ABI correction; its file hash changed, but its entry and both loaded ELF segments match exactly.
The frozen candidate used by public/performance runs and the independent rebuild remain byte-identical.

## Performance

All **88 planned boots completed and were valid**: eleven unchanged fixed-work benchmarks, N1/N4, baseline/candidate
in AB then BA order. Each boot supplied verified cold and warm results. There were no caps, contention, missing
results or changed input hashes. The eight confirmation boots were not triggered by the reviewed diagnostic rule
(>3% slowdown in both orders, considering read bounds, or an isolated >10% slowdown); they were not skipped planned runs.

Positive numbers below mean the candidate was slower. Each cell is the geometric mean of the two paired wall-time
ratios. All individual times, both pair deltas, read bounds and guest-clock observations are retained in
`performance-analysis.json`; the script and raw outputs are beside it.

| Benchmark | N1 cold | N1 warm | N4 cold | N4 warm |
| --- | ---: | ---: | ---: | ---: |
| build | -0.12% | +0.72% | +0.15% | +0.05% |
| zstd | -0.69% | -0.26% | -0.61% | -2.34% |
| hackbench | +1.86% | +1.79% | -3.66% | -2.04% |
| locks | -1.63% | -1.01% | -1.65% | -4.42% |
| luajit | +1.01% | -0.36% | -1.82% | -1.47% |
| luajit-interp | +0.22% | -0.07% | +0.10% | +0.77% |
| mem | -0.48% | -0.05% | +2.46% | -1.02% |
| cray | +0.83% | -0.04% | -0.50% | +1.68% |
| 7zip | -0.00% | +0.04% | -2.06% | +0.74% |
| sqlite | +0.18% | +0.08% | +0.09% | -0.05% |
| avx | +0.07% | +0.38% | +1.10% | -0.62% |

Across the eleven workloads with equal weight, N1 cold/warm changes are **+0.11%/+0.11%** and N4 changes are
**−0.60%/−0.81%**. No cell triggered the predeclared slowdown screen. The largest positive cell mean is N4 memory's
cold run, **+2.46%**; smaller shifts remain unresolved with two pairs. These results do not prove universal zero
cost or a real speedup. Cold/warm phases within one boot are correlated, and read bounds are observation brackets,
not statistical confidence intervals. Fifty-four boots have unavailable or delayed optional asynchronous CPU observations; these are not used
as exact phase CPU measurements. Independent audit reproduced every wall-time pair, bound and aggregate and
verified all 602 input pins and 704 retained output hashes.

These workload tests do not isolate tight ENTER or conversion-store loops. In particular, nesting-zero ENTER now
pays resident-helper cost to perform its previously missing final probe; newly supported conversion stores also
use a resident helper. No universal per-instruction overhead is claimed.

The timing inputs, accepted correctness gate and exclusive-lock caller are frozen under `performance-pins.json`,
`performance-gate.json` and `performance-campaign.py`. The benchmark comparison uses the previously accepted
randomized-frame build as baseline and matched compilers for both variants, with profiling and compile caching off.

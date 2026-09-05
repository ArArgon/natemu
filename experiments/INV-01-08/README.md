# INV-01-08 — arm64 memory-model, atomics-granularity, and cross-thread probe

What it measures, for `Q-01-08` and kill assumption `KA-06`. Method:
[`docs/research/investigations/INV-01-08.md`](../../docs/research/investigations/INV-01-08.md),
"Method" and "Metrics". The normal, negative, and independent replication
artifacts are manifested as `MAN-01-08-01`, `-02`, and `-03` under
`docs/research/manifests/`. Producing code is reviewed revision `d8cb40c`;
normal/negative runs stamped HEAD `407c612` with byte-identical code.

The histogram-reporting amendment passed method return `REV-167`; its
implementation in `src/check.py` and `src/histogram.py` was returned by
independent `REV-182` for late-failure output leakage. The transactional
report correction has self-return `REV-197` and awaits independent return
`REV-198`. Historical code acceptance does
not approve amended code or a new run. The raw producer is unchanged. The
governing Method defines exact inclusive [10/3,30] handling, symmetric zero
cases, compatible-run pooling, and matching-plain exposure. COMPARABLE alone
is not corroboration: only MP/LDAR-STLR/background qualifies in the existing
normal/replication data; user-interactive plain MP has zero relaxed outcomes
in both runs. Historical checker output is preserved, not overwritten.

This is a capability probe. It compares against no baseline, produces no
performance result, and selects no design. Eligible comparisons can supply
bounded corroboration of cited architectural statements; no finite zero
proves prohibition or a probability bound without a justified model.

## Arms

| Group | Arms |
| --- | --- |
| Litmus | Store-buffering (SB) and message-passing (MP), 2 threads, in three variants: plain `LDR`/`STR`, `LDAR`/`STLR`, and `STR` + `DMB ISH` + `LDR`; each at user-interactive and background QoS. 10^6 iterations per arm by default. Every access is inline assembly so the compiler cannot reorder, fold, or elide it; `make disasm` writes the disassembly the code gate checks. Iterations are fenced by a three-phase sense-reversing barrier so the two threads actually overlap and the scoring pass cannot race the peer's next iteration. |
| LSE atomics | `ldadd` and `cas` on one shared counter at 2, 4, and 8 threads, 10^6 operations per thread. The counter's final value is an integrity check: a lost update means the arm is broken. |
| Alignment | 32-bit `ldaddal` at an aligned address (control), at a misaligned address inside one 16-byte granule, across a 16-byte boundary, and across a 64-byte cache line; plus plain `LDR` and `LDAR` across a cache line for contrast. A `SIGBUS`/`SIGSEGV` handler with `siglongjmp` records signal number and `si_code` per case. The handler refuses to swallow any fault raised outside a probe window (`_exit(90)`), so it cannot mask an unrelated defect. |
| Fallback | Four threads doing a read-modify-write on the hardware-refused misaligned address under one global mutex, giving the cost a software fallback would carry. A single contended address cannot be helped by address striping, so striping is not measured; per-address striping is the upgrade path if a fallback ever has to cover many addresses. |
| Round trip | Cross-thread "invalidation" handshake — one thread bumps an epoch, the peer acknowledges after `DMB ISH` — as the host analogue of a cross-vCPU shootdown. 20 000 samples per QoS class. |
| Affinity | `thread_policy_set(THREAD_AFFINITY_POLICY, tag=1)` return code, feeding the `Q-05-06` placement-stratum question. |

QoS arms sample core placement; they do not control it. No output of this
probe is a placement observation.

## Build and run

```sh
make BUILD=/tmp/inv0108            # build outside the repo
make BUILD=/tmp/inv0108 disasm     # instruction-sequence evidence for the code gate
export NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw
./run.sh                           # evidence run
./run.sh --negative                # fault-injection run; the checker must FAIL
```

`run.sh` refuses to start unless `NATEMU_RAW_ROOT` is set, is an existing
directory, and lies outside the repository. Outputs go to
`$NATEMU_RAW_ROOT/INV-01-08/<UTC>-<git short rev>/`. Nothing is written under
`experiments/`. Expect a few minutes of wall time at the default 10^6
iterations.

## Negative control

`--negative` runs the two litmus sides sequentially on one thread. No
interleaving is then possible, so the Arm-permitted relaxed outcome can never
appear — exactly the defect that would make every "not observed" result below
it meaningless. `src/check.py` must report `RESULT: FAIL` (rules `I2`, the
positive control, and `I3`, concurrency) and exit non-zero.

`--negative` reaches only part of the checker, so the fixtures under `tests/`
cover the rest. Every integrity rule is validated by exactly one of them:

| Rule | Validated by |
| --- | --- |
| `I1` exact litmus-arm coverage | `tests/negative-coverage.json` (arm removed) |
| `I2` positive control | `--negative` mode |
| `I3` litmus threads ran concurrently | `--negative` mode |
| `I4` litmus iteration count | `tests/negative.json` (reduced-count run) |
| `I5` no architecture-forbidden relaxed outcome | `tests/forbidden.json`, `tests/negative-coverage.json` |
| `I6` exact LSE-arm coverage | `tests/negative-coverage.json` |
| `I7` LSE atomics lose no updates | `tests/negative-coverage.json` |
| `I8` exact alignment-case coverage | `tests/negative-coverage.json` |
| `I9` aligned-atomic control does not fault | `tests/negative-coverage.json` |
| `I10` ordinary-load contrast does not fault | `tests/negative-coverage.json` |
| `I11` every fault carries a signal number | `tests/negative-coverage.json` |
| `I12` exact round-trip coverage | `tests/negative-coverage.json` |
| `I13` round trip always acknowledged | `tests/negative-coverage.json` |
| `I14` round-trip latency positive | `tests/negative-coverage.json` |

`validate.sh` requires the valid fixture to pass and every negative fixture
to fail. It also runs the pure reporting fixtures below. `tests/valid.json`
is synthetic, not a raw evidence artifact: its formerly inconsistent
200000-count histograms and 1000000 per-arm denominators are now normalized
by scaling histogram cells fivefold, with matching relaxed counts (including
the MP/background relaxed cell). No producer or immutable raw data changed.

## Fixed sample counts

`--iters` scales the litmus arms only. These arms have counts fixed in the
source and are unaffected by it, so a reduced-count development run does not
reduce them:

| Arm | Fixed count |
| --- | --- |
| LSE atomics (6 arms) | 10^6 operations per thread |
| Software fallback | 250 000 operations per thread, 4 threads |
| Invalidation round trip | 20 000 handshakes per QoS class |
| Alignment | 6 cases, executed once each (the outcome is categorical) |

## Checker

`src/check.py result.json` separates three kinds of judgement:

- **Integrity rules (`I*`)** exit non-zero on failure. The load-bearing one is
  `I2`, the positive control: the plain SB litmus must observe the relaxed
  outcome at least once, or the harness cannot expose weak behaviour at all.
  `I5` rejects a relaxed outcome in any arm the cited architecture forbids:
  SB and MP under `DMB ISH`, and SB and MP under `STLR`/`LDAR`. SB under
  `STLR`/`LDAR` is forbidden per `SRC-108` (Pulte et al., POPL 2018), whose
  axiomatic model defines `bob = po; [dmb.full]; po | [L]; po; [A]` and states
  that a store-release before a load-acquire "is required to be observed in
  order by all threads". An earlier revision of this file and of the checker
  asserted the opposite on no source at all; `REV-043` and `REV-045` returned
  that, and it is corrected here.
- **Decision thresholds (`T*`)** are the predeclared `KA-06`-adjacent bars.
  They are reported as MEET/MISS and never fail the checker; a MISS is a
  finding the result gate adjudicates. They are suppressed entirely when any
  integrity rule has failed, so no threshold is ever reported MEET on a value
  from a defective run.
  `T4` is split: `T4a` uses `ns_per_attempt` (executed atomic instructions)
  and `T4b` uses `ns_per_op` (successful increments), because for `cas` under
  contention the two differ by retry amplification and charging retries to the
  instruction would misattribute the scaling.
- **Histogram diagnostics (`H`)** report COMPARABLE, INDETERMINATE or
  INCONCLUSIVE independently of checker success. For each test/strengthened
  variant/QoS, the diagnostic uses the both-observed cell divided by that
  arm's iteration count. With two positive fractions, the symmetric ratio is
  max/min: strictly below 10/3 is COMPARABLE, inclusive [10/3,30] is
  INDETERMINATE, above 30 is INCONCLUSIVE. One zero is unbounded and both
  zeros are undefined; both are INCONCLUSIVE. Classification uses exact
  rational arithmetic; the printed decimal is display only. Per-run matching
  plain and strengthened relaxed counts are printed separately.

All historical `I1`–`I14` predicates and `T1`–`T4` calculations remain
unchanged. An additional `H-input` reporting-validation gate requires four
nonnegative integer histogram cells summing to the positive integer per-arm
iteration count, and equality between the test's relaxed cell and its
`relaxed_observed` count. Invalid reporting input suppresses both histogram
and timing claims; an integrity failure does likewise. This validation is
not a new architectural prohibition. A histogram comparison is labelled
`corroboration=ELIGIBLE (bounded only)` only after all run checks pass, with
COMPARABLE histograms, positive matching-plain exposure and zero strengthened
exposure in that exact comparison's run set. Global plain-SB I2 success cannot
stand in for MP exposure or another QoS. Eligibility is not a result-gate
acceptance, probability bound or proof of prohibition.

The CLI buffers the complete single-run or cohort report before publishing
it. A late input/arithmetic/rendering exception discards that entire buffer
and emits only a nonzero failure with the exception type; exception messages
are not echoed because they may contain partially rendered claims. Ordinary
integrity/H-input failure diagnostics are retained, without any histogram or
timing claims. A successful report's values and predicates are unchanged.

MP is deliberately not an integrity rule. It is far harder to expose than SB
under a lockstep barrier, and a zero count there is reported, not treated as
either a defect or a prohibition.

### Explicit reviewed histogram cohorts

The single-result command never discovers or combines other files. Optional
`python3 src/check.py --cohort /external/path/cohort.json` pools only a
digest-bound, independently accepted, explicitly declared cohort. It sums
both-observed counts and iteration denominators separately for each arm;
it does not average per-run fractions or ratios. Every member must pass all
historical integrity rules and reporting-input checks before **any** pooled
comparison is printed. A failed member suppresses the entire cohort. Negative
and non-normal modes, duplicate paths/result digests/manifest IDs, incomplete
context and differing context are rejected. No timing threshold is pooled.

The envelope has this shape (placeholders are not accepted artifacts):

```json
{
  "schema": "INV-01-08-cohort-v1",
  "cohort": {
    "id": "explicit-cohort-identity",
    "compatibility_justification": "Exact supporting manifests and approved compatibility rationale",
    "runs": [
      {
        "path": "normal/result.json",
        "sha256": "FULL_RESULT_SHA256",
        "manifest": "MAN-01-08-01",
        "context": {
          "host_id": "recorded exact host identity",
          "host_model": "recorded model",
          "os_build": "exact release and build",
          "sdk": "exact SDK and compiler",
          "features": "recorded relevant feature set",
          "signing": "recorded signing, entitlements and sandbox cell",
          "topology": "recorded worker topology and placement limits",
          "stimulus_layout": "recorded stimulus, barrier, jitter and memory layout",
          "method_revision": "approved producing method identity",
          "code_revision": "approved producing code identity"
        }
      },
      {
        "path": "replication/result.json",
        "sha256": "SECOND_FULL_RESULT_SHA256",
        "manifest": "MAN-01-08-03",
        "context": "REPLACE WITH THE SAME COMPLETE CONTEXT OBJECT"
      }
    ]
  },
  "review": {"path": "accepted-cohort-review.md", "sha256": "FULL_REVIEW_SHA256"}
}
```

Paths are absolute or relative to the envelope's directory. Context fields
must be exactly those ten nonempty strings and equal across members;
test/variant/QoS strata remain separate in the actual raw-arm indexing.
Only strictly matching contexts are implemented: even a proposed reviewed
exception to those equalities is rejected and requires a later amendment.
The accepted review must inspect the actual raw artifacts and supporting
manifests, justify compatibility and contain exactly one of each line:

```text
- Review ID: `REV-nnn`
- Independence: `independent`
- Verdict: `accept`
- Cohort SHA-256: `FULL_CANONICAL_COHORT_SHA256`
```

The cohort hash is SHA-256 of UTF-8
`json.dumps(envelope["cohort"], sort_keys=True, separators=(",", ":"), ensure_ascii=True)`.
It covers the full ordered run set, raw hashes, manifest identities, context,
and compatibility rationale; it excludes the review wrapper to avoid a hash
cycle. `src/histogram.py:cohort_digest` implements this convention. Hash the
completed accepted review's exact bytes for the wrapper's `review.sha256`.
Any cohort change requires a new independently accepted exact binding.

This checks explicit review records and byte identity, not reviewer identity
authentication or the truth of arbitrary context strings. Actual independence,
source/manifest support, compatibility and gate acceptance remain mandatory
research-review obligations. A locally fabricated accept record is not
research approval. No existing review implicitly supplies a cohort binding;
the existing two raw runs are not automatically pooled by this implementation.

`python3 tests/histogram-fixtures.py` performs pure non-evidence tests of exact
boundaries, zeros, asymmetric denominators/count-weighted pooling, matching
plain exposure, integrity/input suppression, and cohort identity/context/review
rejection. Scratch JSON/review records are explicitly synthetic, written only
in temporary directories and removed on completion. It runs no host probe.

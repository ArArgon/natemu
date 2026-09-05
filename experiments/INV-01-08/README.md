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
report correction passed independent return REV-198 after self REV-197.
Adversarial self201/independent202 subsequently reopened fallback correctness,
unsafe unexpected-fault termination, and interpretation/instrument dependencies.
F08-2 now has a code submission for self209/independent210 after accepted
source206/method207. Compilation/disassembly and pure fixtures are permitted;
no new native probe/control has run and no code210 acceptance or clock
instrument result is supplied by this submission. Historical acceptance does
not authorize new runs. The
governing Method defines exact inclusive [10/3,30] handling, symmetric zero
cases, compatible-run pooling, and matching-plain exposure. COMPARABLE alone
is not corroboration: only MP/LDAR-STLR/background qualifies in the existing
normal/replication data; user-interactive plain MP has zero relaxed outcomes
in both runs. Historical checker output is preserved, not overwritten.

This is a capability probe. It compares against no baseline, produces no
performance result, and selects no design. Eligible comparisons can supply
bounded corroboration of cited architectural statements; no finite zero
proves prohibition or a probability bound without a justified model.


## F08-2 implementation submission and evidence hold

The normative accepted method is INV-01-08 Method, F08-2 (REV-207). It retains exactly4
fallback workers ×250000 iterations on a fresh target at offset78/width4,
initial0/expected1000000, neighboring canaries, checked mutex init/lock/unlock/
destroy and thread create/join outcomes, per-worker completion/write counts,
safe lifetime ownership, and an observed final-value oracle. Primitive timing
is aggregate create-through-join elapsed/completed operations. No error,
missing oracle, invalid count, nonpositive/inconsistent timing, canary change
or incomplete worker may qualify any timing from a new run.

Required new controls are deterministic omitted-store and injected lock-error
fallback children, a PROT_NONE unexpected-fault child whose handler only calls
_exit(90), and pure malformed/error/late-output fixtures. Parent supervision
records termination and enforces60s fallback,5s unexpected-fault and600s full
run timeouts. A timeout is invalid evidence, not a slow timing. Structured
failure output belongs outside the handler. New controls must remain visibly
injected; a successful control test is not a host behavior finding.

Source206 and method207 passed before this code implementation. Independent
code210 must pass before any native probe/control execution; self209 cannot
authorize its author's execution. Code changes require an applicable return.
The full method requires an exact accepted Q-01-07 same-sender instrument
characterization before timing collection. Historical handshake completion
counts survive, but167ns p99/T3 MEET remains an unqualified diagnostic until
fresh timing under that prerequisite. Correctness-qualified fallback data and
independent replication remain mandatory; no oracle is added to immutable
old raw. P1, full guest semantics and actual oversubscription remain open.

The checker's bob comment now states the two relevant clauses are a subset
of the full primary seven-clause union. I5's strengthened-SB prohibition is
unchanged. Historical raw and the historical independent replication producer
are not rewritten; that replication path is not an F08-2 authorization.

Existing pooled arithmetic independently reviewed by148/167/168 remains a
documentary interpretation of the two identified raw artifacts, not approval
of an actual automated cohort. Reporting198's binding protocol and synthetic
fixtures do not supply an authentic independent cohort review.

## Arms

| Group | Arms |
| --- | --- |
| Litmus | Store-buffering (SB) and message-passing (MP), 2 threads, in three variants: plain `LDR`/`STR`, `LDAR`/`STLR`, and `STR` + `DMB ISH` + `LDR`; each at user-interactive and background QoS. 10^6 iterations per arm by default. Every access is inline assembly so the compiler cannot reorder, fold, or elide it; `make disasm` writes the disassembly the code gate checks. Iterations are fenced by a three-phase sense-reversing barrier to coordinate iteration phases so the scoring pass cannot race the peer's next iteration; it does not measure simultaneous execution. |
| LSE atomics | `ldadd` and `cas` on one shared counter at 2, 4, and 8 threads, 10^6 operations per thread. The counter's final value is an integrity check: a lost update means the arm is broken. |
| Alignment | 32-bit `ldaddal` at an aligned address (control), at a misaligned address inside one 16-byte granule, across a 16-byte boundary, and across a 64-byte cache line; plus plain `LDR` and `LDAR` across a cache line for contrast. A `SIGBUS`/`SIGSEGV` handler with `siglongjmp` records signal number and `si_code` per case. Outside the active probe window the amended handler only calls `_exit(90)`. Within the window, an always-lock-free address guard must also match the4-byte probed range; otherwise it exits90. Expected recovery still uses the prior bounded mechanism. Runtime U1 success remains unobserved pending210. |
| Fallback | Four host workers, one4-byte crossing address and one mutex candidate. Historical raw lacks final-value/mutex-error oracles, so its19.06ns/op is an unqualified diagnostic, not correct-fallback cost or a floor. F08-2 retains correctness/cost testing and all guest compatibility obligations without selecting a strategy. |
| Round trip | Cross-thread "invalidation" handshake — one thread bumps an epoch, the peer acknowledges after `DMB ISH` — as the host analogue of a cross-vCPU shootdown. 20 000 samples per QoS class. |
| Affinity | `thread_policy_set(THREAD_AFFINITY_POLICY, tag=1)` return code, feeding the `Q-05-06` placement-stratum question. |

QoS arms sample core placement; they do not control it. No output of this
probe is a placement observation.

## Build, preflight and gated execution

`sh validate.sh` compiles normal/verification binaries outside Git, checks
the isolated litmus/LSE/alignment instruction bodies, and runs16 retained
reporting tests plus27 F08-2 pure tests. It never launches a native binary.
All fixtures are author-created synthetic inputs, not independent oracle
approval or research evidence. Watchdog tests mock a child; they do not run
the native fault or fallback control.

```sh
make BUILD=/tmp/inv0108-preflight
make BUILD=/tmp/inv0108-preflight disasm
python3 src/supervisor.py --bundle
python3 src/check.py --historical /external/historical/result.json
```

An explicit external BUILD is required; use shell-safe ASCII paths. No default
repository build or recursive clean is supplied. The native executable itself
checks the same authorization before any measurement or result creation.
It refuses absent output/root/authentication metadata. Output is exclusive,
no-follow, under a canonical existing external raw root that neither contains
nor lies inside the repository. A relocated copy cannot silently establish a
new code-root identity: the build records the repository and current revision.

There is currently **no eligible execution authorization**. After genuine210
and instrument gates, the intended command is:

```sh
export NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw
./run.sh --authorization /external/accepted-execution.json \
  --directory /external/existing-parent/new-campaign --campaign
```

The selected directory must not exist. The runner builds both binaries and
disassembles outside Git, captures compiler/SDK/resolved-header hashes,
features/topology, signing, load and power before/after, and runs exactly one
normal, sequential-negative, omitted-store, injected-lock-error and unexpected-
fault child. Exact-child watchdogs use600/600/60/60/5s respectively. Every child
retains stdout/stderr, raw or partial raw, status/timeout, invocation, build,
environment and hash manifest. Any failure stops promotion; no automatic
retry/exclusion/rescue is implemented. Normal raw initially receives only
integrity diagnostics. Only a complete hash-bound campaign, whose control
oracles the checker recomputes, can publish new timing/histogram claims.

`--mode normal|negative|fallback-only|fallback-drop-write|fallback-lock-error|
unexpected-fault` can instead select a single child. Single normal timing
remains held until a complete campaign exists; controls never produce a timing
finding. A validation-kind authorization permits only standalone/control modes
after210, without waiving the clock prerequisite for evidence. Individual
validation runs cannot be assembled into a timing-eligible campaign.

### Exact authorization and instrument binding

`src/authorization.py:BUNDLE_FILES` is the deterministic explicit implementation
file set: Makefile/run/preflight, producer/headers, checker/histogram/fallback/
authorization/supervisor modules, both fixture scripts and four legacy JSON
fixtures. Bundle SHA-256 is the hash of the UTF-8 canonical sorted compact JSON
mapping each relative filename to its exact file SHA-256. README and review
records are not included, avoiding circular code-review hashes; self209 pins
them separately. `supervisor.py --bundle` emits the mapping and digest.

The external authorization object has schema
`INV-01-08-execution-authorization-v1`, `kind` evidence or validation,
`code_revision` equal to the producing current committed clean revision,
`code_bundle_sha256`, and `method_review`/`code_review` references. Each
reference is exactly `{"path": "...", "sha256": "..."}`. Method review is pinned
to the actual independently accepted REV-207 bytes. Code review must be the
actual canonical repository REV-210 with unique accepted independent code
metadata and the exact additional line:

```text
- Code bundle SHA-256: `FULL_BUNDLE_DIGEST`
```

Evidence authorization additionally contains `clock: {packet,
applicability_review}`. Packet schema is
`INV-01-07-clock-prerequisite-v1`, consumer `INV-01-08 F08-2`,
`scope_justification` is a substantive reviewed rationale, `reviews` has exact
method/code/result/replication/adversarial artifact references, and `raw` is
a nonempty list of distinct external artifact references. Those five reviews
must be distinct actual canonical independently accepted records of the
corresponding type. `context` has exactly host_id (IOPlatformUUID), host_model,
os_build, architecture, and timebase {numer,denom}; native authorization
compares these against this host and actual mach_timebase_info before work.
The exact independently accepted applicability result review must contain:

```text
- Clock prerequisite SHA-256: `SHA256_OF_CANONICAL_PACKET`
```

That packet digest binds every method/code/raw/result/replication/adversarial
identity, the consumer and applicability rationale. The applicability reviewer
must inspect those actual artifacts and establish measured granularity,
read-cost/floor and monotonic/conversion adequacy, not merely repeat labels.
The checker verifies real bytes and exact unique review metadata; authenticity,
reviewer identity/independence and factual applicability remain governance
obligations. A fabricated accept record or arbitrary JSON approved flag grants
no research approval. No such actual clock packet, applicability review or
code210 approval is authored here. A future changed producing bundle/revision
needs an appropriate updated accepted binding; this runner does not silently
reuse an old authorization.

## Negative control

`--mode negative` runs the two litmus sides sequentially on one thread. No
interleaving is then possible, so the Arm-permitted relaxed outcome can never
appear — exactly the defect that would make every "not observed" result below
it meaningless. `src/check.py` must report `RESULT: FAIL` (rules `I2`, the
positive control, and `I3`, concurrency) and exit non-zero.

The sequential control reaches only part of the checker, so the fixtures under `tests/`
cover the rest. The retained fixtures cover the historical integrity checks:

| Rule | Validated by |
| --- | --- |
| `I1` exact litmus-arm coverage | `tests/fallback-fixtures.py` direct missing-arm mutation |
| `I2` positive control | `--negative` mode |
| `I3` litmus threads ran concurrently | `--negative` mode |
| `I4` litmus iteration count | `tests/fallback-fixtures.py` direct reduced-count mutation; `S1` also requires exactly10^6 |
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

F08-2 rejects any `--iters` value other than1000000. These unchanged counts
are fixed; no reduced sample substitutes for the full method:

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
  axiomatic model's seven-clause union includes `bob ⊇ po; [dmb.full]; po`
  and `bob ⊇ [L]; po; [A]` (a subset, not the complete definition) and states
  that a store-release before a load-acquire "is required to be observed in
  order by all threads". An earlier revision of this file and of the checker
  asserted the opposite on no source at all; `REV-043` and `REV-045` returned
  that, and it is corrected here.
- **Decision thresholds (`T*`)** are the predeclared `KA-06`-adjacent bars.
  They are reported as MEET/MISS and never fail the checker; a MISS is a
  finding the result gate adjudicates. They are suppressed entirely when any
  integrity rule has failed, so no threshold is ever reported MEET on a value
  from a defective run.
  T4a uses aggregate coordinator elapsed/total attempts; T4b uses the same
  elapsed/total successes. The interval includes creation/join, scheduling,
  loop/load/branch and bookkeeping work across parallel workers. Their ratio
  is the attempts/success denominator identity, not isolated instruction
  latency or causal separation. Both historical primitive T4b ratios
  67.3297069389583×/66.1658439610570× still MISS60×.
- **Histogram diagnostics (`H`)** report COMPARABLE, INDETERMINATE or
  INCONCLUSIVE independently of checker success. For each test/strengthened
  variant/QoS, the diagnostic uses the both-observed cell divided by that
  arm's iteration count. With two positive fractions, the symmetric ratio is
  max/min: strictly below 10/3 is COMPARABLE, inclusive [10/3,30] is
  INDETERMINATE, above 30 is INCONCLUSIVE. One zero is unbounded and both
  zeros are undefined; both are INCONCLUSIVE. Classification uses exact
  rational arithmetic; the printed decimal is display only. Per-run matching
  plain and strengthened relaxed counts are printed separately.

Default checking now requires v2 F1–F4, the fixed full counts (`S1`), successful
supervisor receipt, accepted prior instrument binding and the complete campaign
before I/H/T reporting. F1 checks strict types/bounds/exact primitive conversion;
F2 checks final/write/iteration/mutex oracles; F3 checks lifetime/APIs/canaries;
F4 rejects injected/non-normal/incomplete status. T2 is exact elapsed_ns divided
by completed_ops, strict500ns. The optional rounded display, if present, must
match nearest/ties-to-even2-decimal arithmetic and is never trusted for the
screen. Historical v1 requires `--historical` and emits no qualified T2/T3 or
new F08-2 PASS. T4 ratios use exact integer elapsed/attempt or elapsed/operation
primitives, not rounded display divisions. No future observation is implied.
An additional `H-input` reporting-validation gate requires four
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
timing claims. The existing exact H boundaries and matching-plain predicates are unchanged;
new F08-2 prerequisite/integrity gates only further restrict promotion.

MP is deliberately not an integrity rule. It is far harder to expose than SB
under a lockstep barrier, and a zero count there is reported, not treated as
either a defect or a prohibition.

### Explicit reviewed histogram cohorts

The single-result command never discovers or combines other files. Optional
`python3 src/check.py --cohort /external/path/cohort.json` pools only a
digest-bound, independently accepted, explicitly declared cohort. It sums
both-observed counts and iteration denominators separately for each arm;
it does not average per-run fractions or ratios. Every member must pass its explicit mode's integrity and reporting checks
before any pooled comparison is printed. Default v2 additionally requires
each member's entire F08-2 authorization/campaign; `--historical --cohort`
permits only legacy-v1 members and cannot bypass v2 F gates. A failed member suppresses the entire cohort. Negative
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

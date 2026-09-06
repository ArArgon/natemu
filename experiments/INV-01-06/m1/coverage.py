"""INV-01-06 M06-M1's finite covering design. Importing this module runs no
native code: it only enumerates the coverage manifest and, given results,
checks that all specified cells are present.

All local signing × n × code × access × topology × path cells are retained.
Capability failures are observations, not precomputed exclusions.

n=1 is retained as the diagnostic cell alongside the required n=2/4/8
(INV-01-06.md:715). Warm-up/checked counts and seeds are frozen exactly as
specified: 3 fresh children per positive cell, seeds 60601-60603, 10 warm-up
+ 100 checked events per worker; 3 fresh children per negative/terminal/
restoration arm.
"""
import hashlib
import itertools
import json

METHOD = "M06-M1"
SIGNINGS = ("adhoc-plain", "adhoc-hardened-allow-jit")
NS = (1, 2, 4, 8)
JITS = (0, 1)
ACCESSES = ("load", "store")
TOPOLOGIES = ("serial", "perworker")
PATHS = ("direct", "forward")
POSITIVE_SEEDS = (60601, 60602, 60603)
WARMUP = 10
CHECKED = 100

# Runtime negatives, verbatim from INV-01-06.md:754-758.
RUNTIME_NEGATIVES = (
    "corrupted-repair-result", "skipped-repair-nominal-success",
    "wrong-event-descriptor", "server-stall", "receive-right-destruction",
    "reply-send-failure", "prior-server-failure", "unexpected-helper-fault",
)
# Terminal/restoration/prior-handler arms, from INV-01-06.md:669-673,690-696,709-711.
HOLDS = ("worker-armed-before-fault", "server-after-receive", "server-before-repair",
         "server-before-reply", "front-before-forward", "prior-before-reply",
         "worker-resumed-before-disarm", "teardown-before-restore", "before-right-disposal")

AUXILIARY_ARMS = ("terminal", "restoration", "prior-handler-unsupported")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def cell_eligible(signing, jit):
    # Both local cells are capability questions; lack of hardened runtime
    # does not itself exclude an immutable-JIT allocation.
    return None


def positive_cells():
    found = []
    for signing, n, jit, access, topology, path in itertools.product(
            SIGNINGS, NS, JITS, ACCESSES, TOPOLOGIES, PATHS):
        cell = dict(signing=signing, n=n, jit=jit, access=access,
                    topology=topology, path=path, diagnostic=(n == 1))
        reason = cell_eligible(signing, jit)
        cell["cell_id"] = digest(cell)
        cell["status"] = "invalid" if reason else "planned"
        cell["invalid_reason"] = reason
        found.append(cell)
    return found


def auxiliary_arms():
    rows = []
    for name in RUNTIME_NEGATIVES:
        rows.append(dict(kind="negative", arm=name, arm_id=digest(dict(kind="negative", arm=name))))
    for name in AUXILIARY_ARMS:
        rows.append(dict(kind="terminal" if name == "terminal" else "control", arm=name,
                          arm_id=digest(dict(kind="control", arm=name))))
    return rows


def manifest():
    cells = positive_cells()
    arms = auxiliary_arms()
    holds=[]
    for cell in cells:
        for hold in HOLDS:
            applicable=not (hold in ("front-before-forward","prior-before-reply") and cell["path"]!="forward")
            for worker in range(cell["n"]):
                holds.append(dict(cell_id=cell["cell_id"],hold=hold,worker=worker,
                    status="planned" if applicable else "NA",repetitions=3 if applicable else 0,
                    reason="held-actor controller return requires review" if applicable else "no forwarding path"))
    result = dict(
        investigation="INV-01-06", method=METHOD, routine_id=2406,
        cells=cells, auxiliary_arms=arms, holds=holds,
        positive_sample_plan=dict(children_per_cell=3, seeds=list(POSITIVE_SEEDS),
                                  warmup_events_per_worker=WARMUP,
                                  checked_events_per_worker=CHECKED,
                                  warmup_failures_count=True),
        auxiliary_sample_plan=dict(children_per_arm=3, seeds=list(POSITIVE_SEEDS)),
        eligible_cell_count=sum(1 for c in cells if c["status"] == "planned"),
        invalid_cell_count=sum(1 for c in cells if c["status"] == "invalid"),
        deferred=["P1", "exact-required-P2-model", "Developer-ID", "notarization",
                  "second-machine-replication", "protected-identity-behavior",
                  "arbitrary-foreign-prior-handler", "JIT-write-window-composition",
                  "cost/latency-measurement"],
        first_stage="six plain/non-JIT/serial/direct n=1 children: load/store x three seeds",
        later_stages_require_review=True,
        watchdog_seconds=dict(no_progress=5, total=30),
        no_outcome_driven_replenishment=True,
    )
    result["manifest_sha256"] = digest(result)
    return result


def check_coverage(plan, completed_cell_ids, completed_arm_ids):
    """Every specified cell/arm must be accounted for; nothing pooled or dropped."""
    missing_cells = [c["cell_id"] for c in plan["cells"]
                      if c["status"] == "planned" and c["cell_id"] not in completed_cell_ids]
    missing_arms = [a["arm_id"] for a in plan["auxiliary_arms"] if a["arm_id"] not in completed_arm_ids]
    return dict(complete=not missing_cells and not missing_arms,
                missing_cells=missing_cells, missing_arms=missing_arms)


if __name__ == "__main__":
    print(canonical(manifest()))

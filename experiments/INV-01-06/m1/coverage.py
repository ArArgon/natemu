"""INV-01-06 M06-M1's finite covering design. Importing this module runs no
native code: it only enumerates the coverage manifest and, given results,
checks that all specified cells are present.

Cross-product per INV-01-06.md:765-774: signing x n x non-JIT/immutable-JIT x
load/store x serial/per-worker server x direct/forwarded path. jit=1 requires
the hardened-runtime+allow-jit signing cell (Q-01-04's MAP_JIT toggle needs
that entitlement on a hardened binary); the non-hardened x jit=1 combination
is recorded as an eligible-but-invalid cell, never silently dropped.

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
AUXILIARY_ARMS = ("terminal", "restoration", "prior-handler-unsupported")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def cell_eligible(signing, jit):
    if jit and signing != "adhoc-hardened-allow-jit":
        return "immutable-JIT requires the hardened-runtime+allow-jit signing cell"
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
    result = dict(
        investigation="INV-01-06", method=METHOD, routine_id=2406,
        cells=cells, auxiliary_arms=arms,
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

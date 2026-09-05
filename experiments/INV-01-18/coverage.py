"""M18-2's finite covering design. Importing this module does not run a probe."""
import hashlib
import itertools
import json

METHOD = "M18-2"
ARMS = ("I", "S", "E", "V", "M-data-map", "M-data-protect", "M-jit-protect")
HOOKS = (
    "reader-before-load", "reader-after-load", "reader-after-hazard",
    "reader-after-validation", "reader-in-call", "reader-after-call",
    "reader-before-ack", "reader-after-ack", "writer-partial",
    "writer-written", "writer-maintained", "writer-published",
    "writer-quiescent", "writer-mutated", "writer-before-reuse",
    "translator-before-check", "coordinator-after-check", "producer-before-grant",
)
NEGATIVES = (
    "wrong-generation", "corrupt-return", "stale-translation", "stale-ack",
    "double-grant", "early-retire", "aba", "lost-ack", "unowned-install",
    "trace-missing", "trace-truncated", "trace-duplicate",
)
SENSITIVITY = ("omit-cache", "omit-isb", "early-cache-publication")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def applicable(cell, hook, actor):
    arm, n = cell["arm"], cell["n"]
    base = arm[0]
    if hook.startswith("reader-"):
        if actor >= n:
            return "reader identity outside topology"
        if hook in ("reader-after-load", "reader-after-hazard", "reader-after-validation", "reader-after-call") and base != "I":
            return "hazard/revalidation hook belongs to I"
    if hook == "writer-published" and base != "I":
        return "post-publication hazard scan belongs to I"
    if hook in ("writer-quiescent", "writer-mutated") and base not in ("S", "M"):
        return "no stop-to-mutate interval in this arm"
    if hook == "producer-before-grant" and cell["producers"] != 2:
        return "coordinator is the single writer; V builds on each reader"
    if not hook.startswith("reader-") and hook != "producer-before-grant":
        if base == "V" and hook in ("writer-partial", "writer-written", "writer-maintained"):
            if actor >= n:
                return "V builder identity outside topology"
        elif actor != 0:
            return "coordinator-owned transition"
    if hook == "producer-before-grant" and actor >= 2:
        return "producer identity outside two-producer cell"
    return None


def cells():
    """Deduplicated core + exactly one non-core factor; never Cartesianize."""
    found = {}
    for arm, n, rate in itertools.product(ARMS, (1, 2, 4, 8), (1, 64)):
        core = dict(arm=arm, n=n, rate=rate, signing="A", qos="default",
                    size=256, producers=1, layout="normal", pressure="normal", pages=1)
        variants = [core]
        if n > 1:
            factors = dict(size=(64, 4096), qos=("background",), signing=("B", "C"),
                           layout=("cache-boundary", "page-boundary"),
                           pressure=("near-full", "fragmented"))
            if arm != "V":
                factors["producers"] = (2,)
            if arm.startswith("M-data"):
                factors["pages"] = (4,)
            for key, values in factors.items():
                for value in values:
                    variants.append(dict(core, **{key: value}))
        for cell in variants:
            found[canonical(cell)] = cell
    return [dict(cell, cell_id=digest(cell)) for _, cell in sorted(found.items())]


def manifest():
    rows = cells()
    hooks = []
    for cell in rows:
        for hook in HOOKS:
            for actor in range(max(cell["n"], cell["producers"])):
                reason = applicable(cell, hook, actor)
                hooks.append(dict(cell_id=cell["cell_id"], hook=hook, actor=actor,
                                  status="NA" if reason else "planned", reason=reason,
                                  repetitions=0 if reason else 3))
    result = dict(investigation="INV-01-18", method=METHOD, cells=rows, hooks=hooks,
                  supplement="M18-P1", pressure_stages=["pressure-published-retired-setup","allocator-only","actual-pressure-replacement","hook","warmup","stress"],
                  correctness=dict(seeds=list(range(18001, 18006)), warmup=1024,
                                   publications=4096, calls_per_reader=16384),
                  cost=dict(seeds=list(range(18101, 18106)), batches=1000,
                            pair_count=10000, calibration_batches=64,
                            batch_sizes=[1, 16, 256, 4096]),
                  negatives=list(NEGATIVES), sensitivity=list(SENSITIVITY),
                  protection_controls=["forbidden", "neighbor", "restored"],
                  watchdog_seconds=30, arena_bytes=16 << 20, metadata_trace_cap=256 << 20,
                  deferred=["P1", "exact-required-P2-model", "Developer-ID", "notarization",
                            "sandbox", "executable-remapping", "fault-during-write",
                            "multiple-noncore-factor-interactions", "real-guest-semantics"],
                  cost_requires="accepted same-host INV-01-07 result + local calibration")
    result["manifest_sha256"] = digest(result)
    return result


if __name__ == "__main__":
    print(canonical(manifest()))

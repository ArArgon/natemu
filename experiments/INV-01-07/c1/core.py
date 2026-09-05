"""Exact, raw-first M07-C1 arithmetic. No host measurement or evidence runner.

This module is new research code under the method accepted by REV-194.
It is not independently code-accepted. The artifact layer must separately
enforce the complete fixed matrix, cardinalities, hashes and terminal status.
"""
from fractions import Fraction

U64_MAX = (1 << 64) - 1
U32_MAX = (1 << 32) - 1
CLOCKS = (
    "mach_absolute_time",
    "clock_gettime_nsec_np(CLOCK_UPTIME_RAW)",
    "clock_gettime(CLOCK_MONOTONIC_RAW)",
    "clock_gettime(CLOCK_MONOTONIC)",
    "clock_gettime(CLOCK_UPTIME_RAW)",
    "clock_gettime(CLOCK_REALTIME)",
)
QOS = ("user-interactive", "background")
SEEDS = (70701, 70702, 70703)
READS = 200001
WORKER_READS = 200000
BATCHES = 200
PER_BATCH = 1000
PAIRS = 10000
CONTROLS = 256


class Invalid(ValueError):
    """A named input/integrity defect, not a host performance outcome."""


class Unresolved(ValueError):
    """Required resolution/progress missing; no child finding is eligible."""


def require(condition, code):
    if not condition:
        raise Invalid(code)


def uint(value, code="integer", *, positive=False, maximum=U64_MAX):
    require(type(value) is int and (1 if positive else 0) <= value <= maximum,
            code)
    return value


def timebase(numer, denom):
    return Fraction(uint(numer, "timebase-numer", positive=True, maximum=U32_MAX),
                    uint(denom, "timebase-denom", positive=True, maximum=U32_MAX))


def nearest_rank(values, percentile):
    """One-based ceil(p*N) rank; comparisons retain exact Fractions."""
    require(len(values) > 0, "empty-quantile")
    require(isinstance(percentile, Fraction) and 0 < percentile <= 1,
            "percentile")
    rank = (percentile.numerator * len(values) + percentile.denominator - 1) // percentile.denominator
    return sorted(values)[rank - 1]


def distribution(values):
    require(len(values) > 0, "empty-distribution")
    return {"min": min(values), "p50": nearest_rank(values, Fraction(1, 2)),
            "p99": nearest_rank(values, Fraction(99, 100)), "max": max(values)}


def scan(values):
    """Raw adjacent-difference counts; zeros are never removed."""
    require(len(values) >= 2, "short-scan")
    previous = uint(values[0], "raw-clock-value")
    counts = {"forward": 0, "zero": 0, "backward": 0, "min_positive_step": None}
    for value in values[1:]:
        current = uint(value, "raw-clock-value")
        if current < previous:
            counts["backward"] += 1
        elif current == previous:
            counts["zero"] += 1
        else:
            counts["forward"] += 1
            delta = current - previous
            old = counts["min_positive_step"]
            counts["min_positive_step"] = delta if old is None else min(old, delta)
        previous = current
    require(sum(counts[k] for k in ("forward", "zero", "backward")) == len(values) - 1,
            "scan-cardinality")
    return counts


def checked_clock_scan(values, *, monotonic):
    counts = scan(values)
    if monotonic:
        require(counts["backward"] == 0, "clock-backward")
    if not counts["forward"]:
        raise Unresolved("clock-no-forward-step")
    return counts


def pair_deltas(pairs):
    """Pair identity/cardinality belongs to the artifact layer."""
    result = []
    for pair in pairs:
        require(isinstance(pair, (tuple, list)) and len(pair) == 2, "pair-shape")
        start, end = (uint(v, "pair-endpoint") for v in pair)
        require(end >= start, "pair-backward")
        result.append(end - start)
    require(bool(result), "empty-pairs")
    return result


def batch_costs(batches, pre_pairs, post_pairs, tick_ns):
    require(isinstance(tick_ns, Fraction) and tick_ns > 0, "tick-ratio")
    before = pair_deltas(pre_pairs)
    after = pair_deltas(post_pairs)
    floor = max(100, 20 * nearest_rank(before, Fraction(99, 100)),
                20 * nearest_rank(after, Fraction(99, 100)))
    elapsed = pair_deltas(batches)
    if any(value <= floor for value in elapsed):
        raise Unresolved("batch-at-or-below-floor")
    costs = [Fraction(value, PER_BATCH) * tick_ns for value in elapsed]
    return {"floor_ticks": floor, "zero_pairs_pre": before.count(0),
            "zero_pairs_post": after.count(0), "costs_ns_per_read": costs,
            "summary": distribution(costs)}


def containment(sextuples, tick_ns, resolution_ns):
    require(isinstance(tick_ns, Fraction) and tick_ns > 0, "tick-ratio")
    resolution_ns = uint(resolution_ns, "uptime-resolution", positive=True)
    tick_ceil = (tick_ns.numerator + tick_ns.denominator - 1) // tick_ns.denominator
    slack = 6 * max(tick_ceil, resolution_ns)
    require(slack <= 500, "containment-slack-cap")
    require(bool(sextuples), "empty-containment")
    tight, zero_independent = 0, 0
    minimum_width = None
    for row in sextuples:
        require(isinstance(row, (tuple, list)) and len(row) == 6, "sextuple-shape")
        m0, u0, m1, m2, u1, m3 = (uint(v, "control-endpoint", positive=True) for v in row)
        require(m0 <= m1 <= m2 <= m3 and u1 >= u0, "control-order")
        inner, outer = (m2 - m1) * tick_ns, (m3 - m0) * tick_ns
        require(inner >= 1000000, "control-too-short")
        independent = u1 - u0
        require(inner - slack <= independent <= outer + slack, "containment-violation")
        width = outer - inner
        tight += width <= 500
        zero_independent += independent == 0
        minimum_width = width if minimum_width is None else min(minimum_width, width)
    if not tight:
        raise Unresolved("containment-all-wide")
    return {"slack_ns": slack, "tight_count": tight,
            "minimum_width_ns": minimum_width, "zero_independent": zero_independent}


def contention(records, workers, expected_per_worker=WORKER_READS):
    """Records are (global sequence, worker ID, raw Mach timestamp).

    The optional count is for arithmetic unit tests only. Evidence callers
    must pass WORKER_READS and enforce workers in (1, 2, 4, 8).
    """
    uint(workers, "worker-count", positive=True, maximum=8)
    uint(expected_per_worker, "worker-samples", positive=True)
    require(workers in (1, 2, 4, 8) and expected_per_worker >= 2, "contention-config")
    per_worker = [[] for _ in range(workers)]
    global_values = []
    changes, prior_worker = 0, None
    for sequence, row in enumerate(records):
        require(isinstance(row, (tuple, list)) and len(row) == 3, "trace-shape")
        actual_sequence, worker, timestamp = row
        require(uint(actual_sequence, "trace-sequence") == sequence, "trace-sequence")
        uint(worker, "trace-worker", maximum=workers - 1)
        uint(timestamp, "trace-timestamp")
        per_worker[worker].append(timestamp)
        global_values.append(timestamp)
        if prior_worker is not None and worker != prior_worker:
            changes += 1
        prior_worker = worker
    require(len(global_values) == workers * expected_per_worker, "trace-count")
    require(all(len(v) == expected_per_worker for v in per_worker), "worker-trace-count")
    individual = [scan(values) for values in per_worker]
    global_scan = scan(global_values)
    require(all(c["backward"] == 0 for c in individual), "worker-trace-backward")
    require(global_scan["backward"] == 0, "global-trace-backward")
    if (any(c["forward"] == 0 for c in individual) or global_scan["forward"] == 0
            or (workers > 1 and changes == 0)):
        raise Unresolved("contention-no-progress")
    return {"workers": individual, "global": global_scan,
            "inter_worker_transitions": changes}

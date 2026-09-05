"""Fixed-matrix C1 record validator, independent of native summary arithmetic.

No host calls, printing, file creation, or evidence authorization. The future
artifact/supervisor layer must bind these records to binary arrays, hashes,
actual process outcomes, clean reviewed code, and external provenance.
All findings are returned transactionally only after every required cell.
"""
from fractions import Fraction

import core

SCHEMA = "INV-01-07-clock-c1-v1"
WORKERS = (1, 2, 4, 8)
NEGATIVE_MODES = ("backward", "zero-nsec", "omit-clock", "omit-contention",
                  "pthread-error", "global-inversion")


def fields(value, names, code):
    core.require(type(value) is dict and set(value) == set(names), code)
    return value


def status(value, code):
    core.require(type(value) is int and value == 0, code)


def exact_ratio(value):
    fields(value, ("numerator", "denominator"), "ratio-fields")
    numerator = core.uint(value["numerator"], "ratio-numerator",
                          maximum=core.U64_MAX * core.U32_MAX)
    denominator = core.uint(value["denominator"], "ratio-denominator",
                            positive=True, maximum=core.U64_MAX * core.U32_MAX)
    ratio = Fraction(numerator, denominator)
    core.require((ratio.numerator, ratio.denominator) == (numerator, denominator),
                 "noncanonical-ratio")
    return ratio


def timespec(seconds, nanos):
    core.uint(seconds, "timespec-seconds")
    core.uint(nanos, "timespec-nanoseconds", maximum=999999999)
    return core.uint(seconds * 1000000000 + nanos, "timespec-overflow")


def qos(record, expected):
    fields(record, ("requested", "set_status", "get_status", "get_class",
                    "current_class", "relative_priority"), "qos-fields")
    core.require(expected in core.QOS and record["requested"] == expected
                 and record["get_class"] == expected
                 and record["current_class"] == expected, "qos-mismatch")
    status(record["set_status"], "qos-set")
    status(record["get_status"], "qos-get")
    status(record["relative_priority"], "qos-relative-priority")


def indexed(rows, count, width, code):
    """The sequence ID is explicit, zero-based, and must be contiguous."""
    core.require(len(rows) == count, code + "-count")
    result = []
    for index, row in enumerate(rows):
        core.require(isinstance(row, (tuple, list)) and len(row) == width + 1,
                     code + "-shape")
        core.require(core.uint(row[0], code + "-id") == index, code + "-id")
        result.append(tuple(row[1:]))
    return result


def scan_summary(reported, derived):
    fields(reported, ("forward", "zero", "backward", "min_positive_step"),
           "scan-summary-fields")
    for name in ("forward", "zero", "backward"):
        core.uint(reported[name], "scan-summary-domain")
    if reported["min_positive_step"] is not None:
        core.uint(reported["min_positive_step"], "scan-summary-step", positive=True)
    core.require(reported == derived, "scan-summary-mismatch")


def resolution(record):
    fields(record, ("status", "seconds", "nanoseconds"), "resolution-fields")
    status(record["status"], "resolution-api")
    value = timespec(record["seconds"], record["nanoseconds"])
    return core.uint(value, "resolution-zero", positive=True)


def phase(record, tick, uptime_resolution):
    fields(record, ("noise", "containment"), "phase-fields")
    noise = indexed(record["noise"], core.PAIRS, 2, "noise")
    controls = indexed(record["containment"], core.CONTROLS, 6, "containment")
    return noise, core.containment(controls, tick, uptime_resolution)


def clock_cell(cell, tick):
    fields(cell, ("clock", "qos", "qos_record", "resolution", "uptime_resolution",
                  "readings", "batches", "pre", "post", "scan", "cost_summary",
                  "sink"), "clock-cell-fields")
    name, quality = cell["clock"], cell["qos"]
    core.require(name in core.CLOCKS and quality in core.QOS, "clock-cell-id")
    qos(cell["qos_record"], quality)
    core.uint(cell["sink"], "sink")  # Liveness only, never an oracle.
    uptime_resolution = resolution(cell["uptime_resolution"])
    if name == core.CLOCKS[0]:
        core.require(cell["resolution"] is None, "fabricated-mach-resolution")
        raw_resolution = None
    else:
        raw_resolution = resolution(cell["resolution"])
    if name in (core.CLOCKS[1], core.CLOCKS[4]):
        core.require(raw_resolution == uptime_resolution, "uptime-resolution-mismatch")
    values = []
    if name in core.CLOCKS[:2]:
        for (value,) in indexed(cell["readings"], core.READS, 1, "clock-read"):
            values.append(core.uint(value, "clock-read-value",
                                    positive=name == core.CLOCKS[1]))
    else:
        for sec, nsec, value, api_status in indexed(cell["readings"], core.READS, 4,
                                                   "clock-read"):
            status(api_status, "clock-read-api")
            core.uint(value, "clock-read-value")
            core.require(value == timespec(sec, nsec), "clock-read-conversion")
            values.append(value)
    derived_scan = core.checked_clock_scan(values, monotonic=name != core.CLOCKS[-1])
    scan_summary(cell["scan"], derived_scan)
    before, pre_controls = phase(cell["pre"], tick, uptime_resolution)
    after, post_controls = phase(cell["post"], tick, uptime_resolution)
    batches = indexed(cell["batches"], core.BATCHES, 2, "batch")
    costs = core.batch_costs(batches, before, after, tick)
    fields(cell["cost_summary"], ("min", "p50", "p99", "max"), "cost-summary-fields")
    core.require({key: exact_ratio(value) for key, value in cell["cost_summary"].items()}
                 == costs["summary"], "cost-summary-mismatch")
    return {"clock": name, "qos": quality, "scan": derived_scan,
            "raw_unit": "mach-ticks" if name == core.CLOCKS[0] else "nanoseconds",
            "reported_resolution_ns": raw_resolution, "cost": costs,
            "pre_control": pre_controls, "post_control": post_controls,
            "wall_diagnostic": name == core.CLOCKS[-1]}


def contention_cell(cell):
    fields(cell, ("qos", "workers", "mutex_init_status", "mutex_destroy_status",
                  "worker_records", "trace", "global_scan", "transitions"),
           "contention-cell-fields")
    n = core.uint(cell["workers"], "contention-workers", positive=True, maximum=8)
    core.require(n in WORKERS and cell["qos"] in core.QOS, "contention-cell-id")
    status(cell["mutex_init_status"], "mutex-init")
    status(cell["mutex_destroy_status"], "mutex-destroy")
    records = cell["worker_records"]
    core.require(type(records) is list and len(records) == n, "worker-record-count")
    for worker, record in enumerate(records):
        fields(record, ("id", "qos_record", "create_status", "join_status",
                        "completed", "lock_calls", "unlock_calls", "lock_errors",
                        "unlock_errors", "scan"), "worker-record-fields")
        core.require(core.uint(record["id"], "worker-id") == worker, "worker-id")
        qos(record["qos_record"], cell["qos"])
        for key in ("create_status", "join_status", "lock_errors", "unlock_errors"):
            status(record[key], key)
        for key in ("completed", "lock_calls", "unlock_calls"):
            core.require(core.uint(record[key], key) == core.WORKER_READS,
                         key + "-count")
    derived = core.contention(cell["trace"], n)
    for record, summary in zip(records, derived["workers"]):
        scan_summary(record["scan"], summary)
    scan_summary(cell["global_scan"], derived["global"])
    core.require(core.uint(cell["transitions"], "transitions")
                 == derived["inter_worker_transitions"], "transitions-mismatch")
    return {"qos": cell["qos"], "worker_count": n,
            "worker_scans": derived["workers"], "global": derived["global"],
            "inter_worker_transitions": derived["inter_worker_transitions"]}


def matrix(rows, first, second, code):
    core.require(type(rows) is list and len(rows) == len(first) * len(second),
                 code + "-count")
    seen = set()
    for row in rows:
        core.require(type(row) is dict, code + "-row")
        key = (row.get("qos"), row.get("clock") if code == "clock-matrix"
               else row.get("workers"))
        # Check domains before hashing; unhashable malformed IDs are invalid inputs.
        core.require(type(key[0]) is str and key[0] in first, code + "-id")
        expected_type = str if code == "clock-matrix" else int
        core.require(type(key[1]) is expected_type and key[1] in second, code + "-id")
        core.require(key not in seen, code + "-duplicate")
        seen.add(key)
    core.require(seen == {(q, v) for q in first for v in second}, code + "-coverage")


def validate_child(record):
    """Return findings only for a complete normal child; never emit a prefix.

    This does NOT establish artifact provenance or independent gate acceptance.
    The caller must not equate structural PASS with an accepted instrument.
    """
    fields(record, ("schema", "seed", "mode", "terminal", "timebase",
                    "clock_cells", "contention_cells"), "child-fields")
    core.require(record["schema"] == SCHEMA, "schema")
    core.require(core.uint(record["seed"], "seed") in core.SEEDS, "seed")
    core.require(type(record["mode"]) is str and record["mode"] in ("normal", *NEGATIVE_MODES),
                 "injected-or-unknown-mode")
    terminal = fields(record["terminal"], ("exit_status", "signal", "timeout", "complete"),
                      "terminal-fields")
    status(terminal["exit_status"], "child-exit")
    core.require(terminal["signal"] is None and terminal["timeout"] is False
                 and terminal["complete"] is True, "child-terminal")
    base = fields(record["timebase"], ("status", "numer", "denom"), "timebase-fields")
    status(base["status"], "timebase-api")
    tick = core.timebase(base["numer"], base["denom"])
    matrix(record["clock_cells"], core.QOS, core.CLOCKS, "clock-matrix")
    matrix(record["contention_cells"], core.QOS, WORKERS, "contention-matrix")
    clocks = [clock_cell(cell, tick) for cell in record["clock_cells"]]
    traces = [contention_cell(cell) for cell in record["contention_cells"]]
    # Known negative modes traverse the real raw oracles first. An injection
    # that reaches here was not detected and must never return positive timing.
    core.require(record["mode"] == "normal", "injection-not-detected")
    thresholds = {}
    for quality in core.QOS:
        candidates = [cell for cell in clocks if cell["qos"] == quality
                      and not cell["wall_diagnostic"]]
        best = min(cell["cost"]["summary"]["p50"] for cell in candidates)
        thresholds[quality] = {"best_p50_ns": best, "strict_100ns_meet": best < 100}
    return {"seed": record["seed"], "clocks": clocks, "contention": traces,
            "T1": thresholds, "scope": "complete-child structural check only"}

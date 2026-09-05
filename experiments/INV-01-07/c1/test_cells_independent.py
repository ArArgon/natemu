"""Independent, synthetic fixed-cardinality M07-C1 cells.py fixtures.

Author: /root/adversarial_08, not the cells/core author and consequently not
eligible to independently review this file in REV-196. Expected counts,
ratios and outcomes are derived here without any tested summary helper.
Full fixtures use literal prescribed cardinalities, not patched constants.
Lazy immutable row sequences avoid retaining millions of synthetic tuples;
every required index is still visited by the actual validator.

No native process, host clock, raw file or accepted finding is produced.
Structural tests do not establish API implementation, provenance, binary
serialization, resource/watchdog behavior, fresh execs, replication or gates.
Run: python3 -B experiments/INV-01-07/c1/test_cells_independent.py
"""

import contextlib
import io
import unittest
from collections.abc import Sequence
from fractions import Fraction

import cells
import core


CLOCK_NAMES = (
    "mach_absolute_time", "clock_gettime_nsec_np(CLOCK_UPTIME_RAW)",
    "clock_gettime(CLOCK_MONOTONIC_RAW)", "clock_gettime(CLOCK_MONOTONIC)",
    "clock_gettime(CLOCK_UPTIME_RAW)", "clock_gettime(CLOCK_REALTIME)",
)
QUALITIES = ("user-interactive", "background")


class Rows(Sequence):
    """Exact finite synthetic sequence; edits produce a different sequence."""

    def __init__(self, size, make_row, overrides=None):
        self.size = size
        self.make_row = make_row
        self.overrides = dict(overrides or {})

    def __len__(self):
        return self.size

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(self.size))]
        if index < 0:
            index += self.size
        if not 0 <= index < self.size:
            raise IndexError(index)
        return self.overrides[index] if index in self.overrides else self.make_row(index)

    def changed(self, index, row):
        return Rows(self.size, self.make_row, {**self.overrides, index: row})

    def resized(self, size):
        return Rows(size, self.make_row, self.overrides)


def qos_record(quality):
    return {"requested": quality, "set_status": 0, "get_status": 0,
            "get_class": quality, "current_class": quality, "relative_priority": 0}


def scan_record(forward):
    return {"forward": forward, "zero": 0, "backward": 0, "min_positive_step": 1}


def phase_record():
    # tick1ns, inner1ms, width500ns, U duration1ms, slack6ns.
    return {"noise": Rows(10000, lambda i: (i, 100, 100)),
            "containment": Rows(256, lambda i: (
                i, 100, 1000, 350, 1000350, 1001000, 1000600))}


def clock_record(name=CLOCK_NAMES[0], quality=QUALITIES[0], *, duration=100000,
                 expected_ratio=(100, 1)):
    if name in CLOCK_NAMES[:2]:
        readings = Rows(200001, lambda i: (i, i + 1))
    else:
        readings = Rows(200001, lambda i: (i, 0, i + 1, i + 1, 0))
    numerator, denominator = expected_ratio
    return {
        "clock": name, "qos": quality, "qos_record": qos_record(quality),
        "resolution": None if name == CLOCK_NAMES[0] else {
            "status": 0, "seconds": 0, "nanoseconds": 1},
        "uptime_resolution": {"status": 0, "seconds": 0, "nanoseconds": 1},
        "readings": readings, "batches": Rows(200, lambda i: (i, 1000, 1000 + duration)),
        "pre": phase_record(), "post": phase_record(), "scan": scan_record(200000),
        "cost_summary": {key: {"numerator": numerator, "denominator": denominator}
                         for key in ("min", "p50", "p99", "max")},
        "sink": 0,
    }


def contention_record(workers=1, quality=QUALITIES[0]):
    return {
        "qos": quality, "workers": workers, "mutex_init_status": 0,
        "mutex_destroy_status": 0,
        "worker_records": [{
            "id": worker, "qos_record": qos_record(quality),
            "create_status": 0, "join_status": 0,
            "completed": 200000, "lock_calls": 200000, "unlock_calls": 200000,
            "lock_errors": 0, "unlock_errors": 0, "scan": scan_record(199999),
        } for worker in range(workers)],
        "trace": Rows(workers * 200000, lambda i: (i, i // 200000, i + 1)),
        "global_scan": scan_record(workers * 200000 - 1), "transitions": workers - 1,
    }


def child_record(seed=70701):
    # REALTIME is intentionally10ns while ALL monotonic branches are100ns.
    # The correct result is MISS for both QoS classes despite the wall clock.
    return {
        "schema": "INV-01-07-clock-c1-v1", "seed": seed, "mode": "normal",
        "terminal": {"exit_status": 0, "signal": None, "timeout": False, "complete": True},
        "timebase": {"status": 0, "numer": 1, "denom": 1},
        "clock_cells": [clock_record(name, quality,
                                      duration=10000 if name == CLOCK_NAMES[-1] else 100000,
                                      expected_ratio=(10, 1) if name == CLOCK_NAMES[-1] else (100, 1))
                        for quality in QUALITIES for name in CLOCK_NAMES],
        "contention_cells": [contention_record(n, q) for q in QUALITIES for n in (1, 2, 4, 8)],
    }


class IndependentCells(unittest.TestCase):
    def invalid(self, code, function, *args):
        with self.assertRaises(core.Invalid) as caught:
            function(*args)
        self.assertEqual(str(caught.exception), code)

    def test_complete_fixed_matrix_exact100_excludes_faster_realtime(self):
        record = child_record()
        self.assertEqual(len(record["clock_cells"]), 12)
        self.assertEqual(len(record["contention_cells"]), 8)
        self.assertEqual(sum(len(c["trace"]) for c in record["contention_cells"]), 6000000)
        for cell in record["clock_cells"]:
            self.assertEqual(len(cell["readings"]), 200001)
            self.assertEqual(len(cell["batches"]), 200)
            for phase in ("pre", "post"):
                self.assertEqual(len(cell[phase]["noise"]), 10000)
                self.assertEqual(len(cell[phase]["containment"]), 256)
        result = cells.validate_child(record)
        self.assertEqual(result["T1"], {
            q: {"best_p50_ns": Fraction(100), "strict_100ns_meet": False} for q in QUALITIES})
        self.assertEqual(len(result["clocks"]), 12)
        self.assertEqual(len(result["contention"]), 8)
        for c in result["clocks"]:
            self.assertEqual(c["scan"], scan_record(200000))
            self.assertEqual(c["wall_diagnostic"], c["clock"] == CLOCK_NAMES[-1])
            self.assertEqual(c["raw_unit"], "mach-ticks" if c["clock"] == CLOCK_NAMES[0]
                             else "nanoseconds")
            self.assertEqual(c["cost"]["zero_pairs_pre"], 10000)
        for c in result["contention"]:
            self.assertEqual(set(c), {"qos", "worker_count", "worker_scans", "global",
                                     "inter_worker_transitions"})
            self.assertIs(type(c["worker_count"]), int)
            self.assertEqual(len(c["worker_scans"]), c["worker_count"])
            self.assertEqual(c["worker_scans"], [scan_record(199999)] * c["worker_count"])
            self.assertEqual(c["global"], scan_record(c["worker_count"] * 200000 - 1))
            self.assertEqual(c["inter_worker_transitions"], c["worker_count"] - 1)

    def test_complete_matrix_separate_qos_no_cross_minimum(self):
        record = child_record(70702)
        record["clock_cells"][0] = clock_record(duration=99999, expected_ratio=(99999, 1000))
        result = cells.validate_child(record)
        self.assertEqual(result["seed"], 70702)
        self.assertEqual(result["T1"][QUALITIES[0]], {
            "best_p50_ns": Fraction(99999, 1000), "strict_100ns_meet": True})
        self.assertEqual(result["T1"][QUALITIES[1]], {
            "best_p50_ns": Fraction(100), "strict_100ns_meet": False})

    def test_late_required_cell_failure_has_no_partial_return_or_output(self):
        record = child_record(70703)
        # Every clock and the first seven contention cells are valid. This is
        # a required final cell, not a removed matrix row or malformed header.
        record["contention_cells"][-1]["worker_records"][-1]["join_status"] = 7
        stdout, stderr = io.StringIO(), io.StringIO()
        marker = object()
        returned = marker
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaisesRegex(core.Invalid, "^join_status$"):
                returned = cells.validate_child(record)
        self.assertIs(returned, marker)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_missing_extra_duplicate_and_malformed_matrix_ids(self):
        for key, code, id_key in (("clock_cells", "clock-matrix", "clock"),
                                   ("contention_cells", "contention-matrix", "workers")):
            for delta in (-1, 1):
                record = child_record()
                if delta < 0:
                    record[key].pop()
                else:
                    record[key].append(record[key][0])
                self.invalid(code + "-count", cells.validate_child, record)
            record = child_record()
            record[key][-1] = record[key][0]
            self.invalid(code + "-duplicate", cells.validate_child, record)
            for bad in (True, None, [], {}, "unknown"):
                record = child_record()
                record[key][0][id_key] = bad
                self.invalid(code + "-id", cells.validate_child, record)
            record = child_record()
            record[key][0]["qos"] = []
            self.invalid(code + "-id", cells.validate_child, record)

    def test_header_terminal_timebase_status_and_domain(self):
        for key, bad, code in (("schema", "wrong", "schema"),
                              ("seed", True, "seed"), ("seed", 0, "seed"),
                              ("mode", "negative", "injected-or-unknown-mode")):
            record = child_record()
            record[key] = bad
            self.invalid(code, cells.validate_child, record)
        for key, bad, code in (("exit_status", True, "child-exit"),
                              ("exit_status", 1, "child-exit"),
                              ("signal", 0, "child-terminal"),
                              ("timeout", 0, "child-terminal"),
                              ("timeout", True, "child-terminal"),
                              ("complete", 1, "child-terminal"),
                              ("complete", False, "child-terminal")):
            record = child_record()
            record["terminal"][key] = bad
            self.invalid(code, cells.validate_child, record)
        for key, bad, code in (("status", True, "timebase-api"),
                              ("status", 1, "timebase-api"),
                              ("numer", 0, "timebase-numer"),
                              ("denom", 2**32, "timebase-denom")):
            record = child_record()
            record["timebase"][key] = bad
            self.invalid(code, cells.validate_child, record)
        for extra in (False, True):
            record = child_record()
            if extra:
                record["unreviewed"] = 1
            else:
                del record["terminal"]
            self.invalid("child-fields", cells.validate_child, record)

    def test_qos_resolution_and_sink_domains(self):
        for key, value, code in (("set_status", False, "qos-set"),
                                 ("get_status", 1, "qos-get"),
                                 ("relative_priority", True, "qos-relative-priority"),
                                 ("current_class", QUALITIES[1], "qos-mismatch")):
            cell = clock_record()
            cell["qos_record"][key] = value
            self.invalid(code, cells.clock_cell, cell, Fraction(1))
        for value in (-1, True, 2**64):
            cell = clock_record()
            cell["sink"] = value
            self.invalid("sink", cells.clock_cell, cell, Fraction(1))
        cell = clock_record()
        cell["resolution"] = {"status": 0, "seconds": 0, "nanoseconds": 1}
        self.invalid("fabricated-mach-resolution", cells.clock_cell, cell, Fraction(1))
        for key, value, code in (("status", True, "resolution-api"),
                                 ("nanoseconds", 0, "resolution-zero"),
                                 ("nanoseconds", 1000000000, "timespec-nanoseconds")):
            cell = clock_record()
            cell["uptime_resolution"][key] = value
            self.invalid(code, cells.clock_cell, cell, Fraction(1))
        cell = clock_record(CLOCK_NAMES[1])
        cell["resolution"]["nanoseconds"] = 2
        self.invalid("uptime-resolution-mismatch", cells.clock_cell, cell, Fraction(1))

    def test_reading_count_id_api_timespec_and_nsec_zero(self):
        for count in (0, 200000, 200002):
            cell = clock_record()
            cell["readings"] = cell["readings"].resized(count)
            self.invalid("clock-read-count", cells.clock_cell, cell, Fraction(1))
        for row, code in (((True, 1), "clock-read-id"),
                          ((1, 1), "clock-read-id"),
                          ((0, True), "clock-read-value"),
                          ((0, 1, 2), "clock-read-shape")):
            cell = clock_record()
            cell["readings"] = cell["readings"].changed(0, row)
            self.invalid(code, cells.clock_cell, cell, Fraction(1))
        for row, code in (((0, 0, 1, 1, True), "clock-read-api"),
                          ((0, -1, 1, 1, 0), "timespec-seconds"),
                          ((0, 0, 1000000000, 1, 0), "timespec-nanoseconds"),
                          ((0, 18446744074, 0, 1, 0), "timespec-overflow"),
                          ((0, 0, 1, 2, 0), "clock-read-conversion")):
            cell = clock_record(CLOCK_NAMES[2])
            cell["readings"] = cell["readings"].changed(0, row)
            self.invalid(code, cells.clock_cell, cell, Fraction(1))
        cell = clock_record(CLOCK_NAMES[1])
        cell["readings"] = cell["readings"].changed(0, (0, 0))
        self.invalid("clock-read-value", cells.clock_cell, cell, Fraction(1))

    def test_timespec_exact_u64_boundary(self):
        self.assertEqual(cells.timespec(18446744073, 709551615), 2**64 - 1)
        self.invalid("timespec-overflow", cells.timespec, 18446744073, 709551616)
        for seconds, nanos, code in ((True, 0, "timespec-seconds"),
                                    (0, False, "timespec-nanoseconds"),
                                    (0, -1, "timespec-nanoseconds")):
            self.invalid(code, cells.timespec, seconds, nanos)

    def test_wall_clock_backward_is_visible_not_monotonic_qualification(self):
        wall = clock_record(CLOCK_NAMES[-1])
        wall["readings"] = wall["readings"].changed(1, (1, 0, 0, 0, 0))
        wall["scan"] = {"forward": 199999, "zero": 0, "backward": 1,
                        "min_positive_step": 1}
        result = cells.clock_cell(wall, Fraction(1))
        self.assertIs(result["wall_diagnostic"], True)
        self.assertEqual(result["scan"], {"forward": 199999, "zero": 0,
                                           "backward": 1, "min_positive_step": 1})
        monotonic = clock_record(CLOCK_NAMES[2])
        monotonic["readings"] = monotonic["readings"].changed(1, (1, 0, 0, 0, 0))
        self.invalid("clock-backward", cells.clock_cell, monotonic, Fraction(1))

    def test_phase_batch_counts_ids_and_duplicate_samples(self):
        for part, code, count in (("noise", "noise", 10000),
                                  ("containment", "containment", 256)):
            for wrong in (0, count - 1, count + 1):
                phase = phase_record()
                phase[part] = phase[part].resized(wrong)
                self.invalid(code + "-count", cells.phase, phase, Fraction(1), 1)
            for bad_id in (True, 0):
                phase = phase_record()
                original = phase[part][1]
                phase[part] = phase[part].changed(1, (bad_id,) + original[1:])
                self.invalid(code + "-id", cells.phase, phase, Fraction(1), 1)
        cell = clock_record()
        cell["batches"] = cell["batches"].resized(199)
        self.invalid("batch-count", cells.clock_cell, cell, Fraction(1))
        cell = clock_record()
        cell["batches"] = cell["batches"].changed(199, (198, 1000, 101000))
        self.invalid("batch-id", cells.clock_cell, cell, Fraction(1))

    def test_summary_bool_negative_mismatch_and_exact_ratio_domains(self):
        derived = {"forward": 1, "zero": 0, "backward": 0, "min_positive_step": 1}
        for key, value, code in (("forward", True, "scan-summary-domain"),
                                 ("zero", -1, "scan-summary-domain"),
                                 ("backward", 2**64, "scan-summary-domain"),
                                 ("min_positive_step", False, "scan-summary-step"),
                                 ("min_positive_step", 0, "scan-summary-step"),
                                 ("forward", 2, "scan-summary-mismatch")):
            reported = dict(derived)
            reported[key] = value
            self.invalid(code, cells.scan_summary, reported, derived)
        self.assertEqual(cells.exact_ratio({"numerator": 99999, "denominator": 1000}),
                         Fraction(99999, 1000))
        for numerator, denominator, code in ((True, 1, "ratio-numerator"),
                                             (-1, 1, "ratio-numerator"),
                                             (float("nan"), 1, "ratio-numerator"),
                                             (float("inf"), 1, "ratio-numerator"),
                                             (1.0, 1, "ratio-numerator"),
                                             (1, 0, "ratio-denominator"),
                                             (1, True, "ratio-denominator"),
                                             (2, 2, "noncanonical-ratio"),
                                             (0, 2, "noncanonical-ratio")):
            self.invalid(code, cells.exact_ratio,
                         {"numerator": numerator, "denominator": denominator})
        cell = clock_record()
        cell["cost_summary"]["p99"] = {"numerator": 99, "denominator": 1}
        self.invalid("cost-summary-mismatch", cells.clock_cell, cell, Fraction(1))

    def test_worker_api_counts_ids_and_mutex_failures(self):
        for key, value, code in (("mutex_init_status", True, "mutex-init"),
                                 ("mutex_destroy_status", 1, "mutex-destroy"),
                                 ("workers", True, "contention-workers")):
            cell = contention_record()
            cell[key] = value
            self.invalid(code, cells.contention_cell, cell)
        for key, value, code in (("id", True, "worker-id"), ("id", 1, "worker-id"),
                                 ("create_status", 1, "create_status"),
                                 ("join_status", False, "join_status"),
                                 ("lock_errors", -1, "lock_errors"),
                                 ("unlock_errors", 1, "unlock_errors"),
                                 ("completed", 0, "completed-count"),
                                 ("lock_calls", True, "lock_calls"),
                                 ("unlock_calls", 199999, "unlock_calls-count")):
            cell = contention_record()
            cell["worker_records"][0][key] = value
            self.invalid(code, cells.contention_cell, cell)
        cell = contention_record(2)
        cell["worker_records"][1]["id"] = 0
        self.invalid("worker-id", cells.contention_cell, cell)
        cell = contention_record()
        cell["worker_records"] = []
        self.invalid("worker-record-count", cells.contention_cell, cell)

    def test_contention_real_count_lost_duplicate_and_summary_mutations(self):
        cell = contention_record()
        cell["trace"] = cell["trace"].resized(199999)
        self.invalid("trace-count", cells.contention_cell, cell)
        cell = contention_record()
        cell["trace"] = cell["trace"].changed(199999, (199998, 0, 200000))
        self.invalid("trace-sequence", cells.contention_cell, cell)
        cell = contention_record()
        cell["global_scan"]["forward"] = 199998
        self.invalid("scan-summary-mismatch", cells.contention_cell, cell)
        cell = contention_record()
        cell["transitions"] = True
        self.invalid("transitions", cells.contention_cell, cell)


if __name__ == "__main__":
    unittest.main(verbosity=2)

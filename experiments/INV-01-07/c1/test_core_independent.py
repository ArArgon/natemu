"""Independent synthetic arithmetic fixtures for M07-C1 (REV-194).

Authored by /root/adversarial_08, separately from core.py's author. Expected
values below are literal hand-derived integers/Fractions, not core helpers
used as an oracle. Reduced contention traces test arithmetic only; they are
not the required 200000-sample/worker evidence. No host clock is read, native
probe launched, raw artifact written, or finding qualified by these tests.

These do NOT validate full artifact coverage/IDs/statuses, API/timespec
serialization, emitted summaries, transactional reporting, native pthread
paths, resource bounds, watchdogs, root enforcement, or fresh-child evidence.
Those obligations require the separate artifact/native/code-review gates.
This fixture author cannot independently approve these fixtures in REV-196.

Run: python3 -B experiments/INV-01-07/c1/test_core_independent.py
"""

import unittest
from fractions import Fraction

import core


class IndependentArithmetic(unittest.TestCase):
    def invalid(self, code, function, *args, **kwargs):
        with self.assertRaises(core.Invalid) as caught:
            function(*args, **kwargs)
        self.assertEqual(str(caught.exception), code)

    def unresolved(self, code, function, *args, **kwargs):
        with self.assertRaises(core.Unresolved) as caught:
            function(*args, **kwargs)
        self.assertEqual(str(caught.exception), code)

    def test_nearest_rank_small_and_exact_boundaries(self):
        values = [Fraction(11, 3), Fraction(1, 3), Fraction(7, 3),
                  Fraction(5, 3)]
        for percentile, expected in (
                (Fraction(1, 4), Fraction(1, 3)),
                (Fraction(1, 2), Fraction(5, 3)),
                (Fraction(501, 1000), Fraction(7, 3)),
                (Fraction(99, 100), Fraction(11, 3)),
                (Fraction(1), Fraction(11, 3))):
            with self.subTest(percentile=percentile):
                self.assertEqual(core.nearest_rank(values, percentile), expected)
        self.assertEqual(values[0], Fraction(11, 3), "input must not be sorted in place")

    def test_distribution_200_values_does_not_interpolate_or_round(self):
        # Sorted values are 1/3,...,200/3: ranks100 and198 are literal.
        values = [Fraction(n, 3) for n in range(200, 0, -1)]
        self.assertEqual(core.distribution(values), {
            "min": Fraction(1, 3), "p50": Fraction(100, 3),
            "p99": Fraction(66), "max": Fraction(200, 3)})
        self.assertEqual(core.distribution([Fraction(2, 7)]), {
            key: Fraction(2, 7) for key in ("min", "p50", "p99", "max")})

    def test_quantile_domains(self):
        self.invalid("empty-quantile", core.nearest_rank, [], Fraction(1, 2))
        self.invalid("empty-distribution", core.distribution, [])
        for p in (Fraction(0), Fraction(-1), Fraction(1001, 1000), .5, True):
            with self.subTest(percentile=p):
                self.invalid("percentile", core.nearest_rank, [1], p)

    def test_unsigned_integer_domains_without_float_coercion(self):
        for value in (0, 1, 2**64 - 1):
            self.assertEqual(core.uint(value), value)
        for value in (-1, 2**64, True, False, 1.0, "1", None):
            with self.subTest(value=value):
                self.invalid("integer", core.uint, value)
        self.invalid("integer", core.uint, 0, positive=True)

    def test_timebase_exact_and_uint32_boundaries(self):
        self.assertEqual(core.timebase(125, 3), Fraction(125, 3))
        self.assertEqual(core.timebase(2**32 - 1, 2**32 - 1), Fraction(1))
        self.assertEqual(core.timebase(1, 2**32 - 1), Fraction(1, 4294967295))
        for value in (0, -1, True, 3.0, 2**32):
            with self.subTest(value=value):
                self.invalid("timebase-numer", core.timebase, value, 1)
                self.invalid("timebase-denom", core.timebase, 1, value)

    def test_pair_zero_retention_and_no_absolute_endpoint_rounding(self):
        pairs = [(2**64 - 2, 2**64 - 1), (77, 77), (1, 2)]
        self.assertEqual(core.pair_deltas(pairs), [1, 0, 1])
        # Delta conversion gives125/3; converting/flooring endpoints separately
        # at1 and2 ticks would give42, which is deliberately not the oracle.
        self.assertEqual(Fraction(core.pair_deltas([(1, 2)])[0] * 125, 3),
                         Fraction(125, 3))

    def test_pair_invalid_domains_order_and_empty(self):
        self.invalid("empty-pairs", core.pair_deltas, [])
        self.invalid("pair-shape", core.pair_deltas, [(1, 2, 3)])
        self.invalid("pair-backward", core.pair_deltas, [(2, 1)])
        for value in (-1, True, 2**64):
            self.invalid("pair-endpoint", core.pair_deltas, [(0, value)])

    def test_floor_equality_rejects_entire_batch_set(self):
        noise = [(100, 100)] * 10000
        batches = [(1000, 1101)] * 200
        result = core.batch_costs(batches, noise, noise, Fraction(125, 3))
        self.assertEqual(result["floor_ticks"], 100)
        self.assertEqual(result["zero_pairs_pre"], 10000)
        self.assertEqual(result["zero_pairs_post"], 10000)
        self.assertEqual(result["costs_ns_per_read"], [Fraction(101, 24)] * 200)
        batches[-1] = (1000, 1100)  # exactly F, not only the first batch
        self.unresolved("batch-at-or-below-floor", core.batch_costs,
                        batches, noise, noise, Fraction(125, 3))
        batches[-1] = (1000, 1099)
        self.unresolved("batch-at-or-below-floor", core.batch_costs,
                        batches, noise, noise, Fraction(125, 3))

    def test_both_noise_phases_and_p99_zero_rank(self):
        # N10000 p99 rank9900: exactly9900 zeros yields p99=0;9899 does not.
        zeros_at_rank = [(0, 0)] * 9900 + [(0, 7)] * 100
        positive_at_rank = [(0, 0)] * 9899 + [(0, 7)] * 101
        batches = [(0, 141)] * 200
        result = core.batch_costs(batches, zeros_at_rank, positive_at_rank, Fraction(1))
        self.assertEqual(result["floor_ticks"], 140)
        self.assertEqual((result["zero_pairs_pre"], result["zero_pairs_post"]),
                         (9900, 9899))
        reverse = core.batch_costs(batches, positive_at_rank, zeros_at_rank, Fraction(1))
        self.assertEqual(reverse["floor_ticks"], 140)
        self.assertEqual(reverse["summary"]["p99"], Fraction(141, 1000))

    def test_p50_exact_100ns_is_not_strictly_below_screen(self):
        noise = [(0, 0)] * 10000
        for ticks, expected, meet in ((2399, Fraction(2399, 24), True),
                                      (2400, Fraction(100), False),
                                      (2401, Fraction(2401, 24), False)):
            with self.subTest(ticks=ticks):
                result = core.batch_costs([(0, ticks)] * 200, noise, noise,
                                         Fraction(125, 3))
                self.assertEqual(result["summary"]["p50"], expected)
                # Arithmetic boundary only: production T1 selection/reporting
                # is outside this core module and needs artifact-level tests.
                self.assertEqual(result["summary"]["p50"] < 100, meet)

    def test_batch_tick_ratio_and_empty_inputs(self):
        for ratio in (Fraction(0), Fraction(-1), 1.0, True):
            self.invalid("tick-ratio", core.batch_costs, [(0, 101)],
                         [(0, 0)], [(0, 0)], ratio)
        for batches, pre, post in (([], [(0, 0)], [(0, 0)]),
                                   ([(0, 101)], [], [(0, 0)]),
                                   ([(0, 101)], [(0, 0)], [])):
            self.invalid("empty-pairs", core.batch_costs,
                         batches, pre, post, Fraction(1))

    def test_scan_preserves_zeros_and_exact_counts(self):
        self.assertEqual(core.scan([10, 10, 14, 13, 13, 18]), {
            "forward": 2, "zero": 2, "backward": 1, "min_positive_step": 4})
        values = [0] * 100001 + [1] * 100000  # actual200001 cardinality
        self.assertEqual(core.checked_clock_scan(values, monotonic=True), {
            "forward": 1, "zero": 199999, "backward": 0, "min_positive_step": 1})

    def test_monotonic_failure_wall_clock_diagnostic_and_flat(self):
        self.invalid("clock-backward", core.checked_clock_scan,
                     [10, 11, 9, 12], monotonic=True)
        self.assertEqual(core.checked_clock_scan([10, 11, 9, 12], monotonic=False), {
            "forward": 2, "zero": 0, "backward": 1, "min_positive_step": 1})
        for monotonic in (False, True):
            self.unresolved("clock-no-forward-step", core.checked_clock_scan,
                            [7, 7, 7], monotonic=monotonic)
        self.invalid("short-scan", core.scan, [1])
        for value in (-1, True, 2**64):
            self.invalid("raw-clock-value", core.scan, [0, value])

    def test_containment_exact_slack_edges_and_500ns_width(self):
        # Tick125/3: inner24000ticks=1ms; outer24012ticks=1000500ns.
        # ceil(tick)=42, resolution1 => S252. U interval edges999748/1000752.
        for delta in (999748, 1000752):
            row = (100, 1000, 106, 24106, 1000 + delta, 24112)
            self.assertEqual(core.containment([row], Fraction(125, 3), 1), {
                "slack_ns": 252, "tight_count": 1,
                "minimum_width_ns": Fraction(500), "zero_independent": 0})
        for delta in (999747, 1000753):
            row = (100, 1000, 106, 24106, 1000 + delta, 24112)
            self.invalid("containment-violation", core.containment,
                         [row], Fraction(125, 3), 1)

    def test_containment_plus_minus_one_percent_mutations(self):
        # Base:2ms inner,500ns width.±1% is20us, not hideable in252ns slack.
        row = (100, 1000, 106, 48106, 2001000, 48112)
        self.assertEqual(core.containment([row], Fraction(125, 3), 1)["tight_count"], 1)
        for wrong_tick in (Fraction(165, 4), Fraction(505, 12)):
            self.invalid("containment-violation", core.containment, [row], wrong_tick, 1)
        for wrong_delta in (1980000, 2020000):
            altered = (100, 1000, 106, 48106, 1000 + wrong_delta, 48112)
            self.invalid("containment-violation", core.containment,
                         [altered], Fraction(125, 3), 1)

    def test_containment_all_wide_and_every_sample_required(self):
        tight = (100, 1000, 350, 1000350, 1001000, 1000600)
        wide = (100, 1000, 350, 1000350, 1001000, 1000601)
        self.unresolved("containment-all-wide", core.containment,
                        [wide] * 256, Fraction(1), 1)
        result = core.containment([wide] * 255 + [tight], Fraction(1), 1)
        self.assertEqual(result, {"slack_ns": 6, "tight_count": 1,
                                  "minimum_width_ns": Fraction(500), "zero_independent": 0})
        bad = (100, 1000, 350, 1000350, 1002000, 1000600)
        self.invalid("containment-violation", core.containment,
                     [tight] * 255 + [bad], Fraction(1), 1)

    def test_containment_short_zero_and_domain_failures(self):
        good = (100, 1000, 100, 1000100, 1001000, 1000100)
        self.assertEqual(core.containment([good], Fraction(1), 83)["slack_ns"], 498)
        self.invalid("containment-slack-cap", core.containment, [good], Fraction(1), 84)
        self.invalid("containment-slack-cap", core.containment, [good], Fraction(84), 1)
        short = (100, 1000, 100, 1000099, 1000999, 1000099)
        self.invalid("control-too-short", core.containment, [short], Fraction(1), 1)
        flat_u = (100, 1000, 100, 1000100, 1000, 1000100)
        self.invalid("containment-violation", core.containment, [flat_u], Fraction(1), 1)
        self.invalid("empty-containment", core.containment, [], Fraction(1), 1)
        for value in (0, -1, True, 2**64):
            self.invalid("uptime-resolution", core.containment, [good], Fraction(1), value)
            self.invalid("control-endpoint", core.containment,
                         [(value,) + good[1:]], Fraction(1), 1)
        self.invalid("sextuple-shape", core.containment, [good[:-1]], Fraction(1), 1)
        self.invalid("control-order", core.containment,
                     [(101,) + good[1:]], Fraction(1), 1)

    def test_contention_valid_one_two_four_eight_workers_without_alternation(self):
        for workers in (1, 2, 4, 8):
            # Each worker acquires three times consecutively. No assumed
            # alternation;2 positive steps/worker,3n-1 global positive steps.
            records = [(i, i // 3, 100 + i) for i in range(3 * workers)]
            result = core.contention(records, workers, expected_per_worker=3)
            self.assertEqual(result["workers"], [{
                "forward": 2, "zero": 0, "backward": 0,
                "min_positive_step": 1}] * workers)
            self.assertEqual(result["global"], {
                "forward": 3 * workers - 1, "zero": 0,
                "backward": 0, "min_positive_step": 1})
            self.assertEqual(result["inter_worker_transitions"], workers - 1)

    def test_contention_global_only_inversion_preserves_ids_and_worker_order(self):
        # Full IDs and counts; worker0:100,101,102; worker1:101,104,105.
        # Only acquisition transition102->101 is backward.
        records = [(0, 0, 100), (1, 0, 101), (2, 0, 102),
                   (3, 1, 101), (4, 1, 104), (5, 1, 105)]
        self.invalid("global-trace-backward", core.contention, records, 2, 3)

    def test_contention_per_worker_inversion_preserves_ids_and_counts(self):
        records = [(0, 0, 100), (1, 1, 101), (2, 0, 99),
                   (3, 1, 103), (4, 0, 104), (5, 1, 105)]
        self.invalid("worker-trace-backward", core.contention, records, 2, 3)

    def test_contention_flat_worker_global_and_single_worker(self):
        # Global progress and other-worker progress cannot rescue worker0.
        rows = [(0, 0, 100), (1, 0, 100), (2, 0, 100),
                (3, 1, 101), (4, 1, 102), (5, 1, 103)]
        self.unresolved("contention-no-progress", core.contention, rows, 2, 3)
        self.unresolved("contention-no-progress", core.contention,
                        [(i, i // 3, 100) for i in range(6)], 2, 3)
        self.unresolved("contention-no-progress", core.contention,
                        [(i, 0, 100) for i in range(3)], 1, 3)

    def test_contention_zero_counts_retained_with_progress(self):
        rows = [(0, 0, 10), (1, 0, 10), (2, 0, 11),
                (3, 1, 11), (4, 1, 11), (5, 1, 12)]
        result = core.contention(rows, 2, 3)
        self.assertEqual(result["global"], {
            "forward": 2, "zero": 3, "backward": 0, "min_positive_step": 1})
        self.assertEqual(result["workers"], [{
            "forward": 1, "zero": 1, "backward": 0, "min_positive_step": 1}] * 2)

    def test_contention_zero_bad_counts_ids_and_duplicate_sequences(self):
        good = [(0, 0, 100), (1, 0, 101), (2, 1, 102), (3, 1, 103)]
        for count in (0, -1, True, 2**64):
            self.invalid("worker-samples", core.contention, good, 2, count)
        self.invalid("contention-config", core.contention, good, 2, 1)
        self.invalid("worker-count", core.contention, good, 0, 2)
        self.invalid("contention-config", core.contention, good, 3, 2)
        self.invalid("trace-count", core.contention, good[:-1], 2, 2)
        self.invalid("worker-trace-count", core.contention,
                     [(0, 0, 100), (1, 0, 101), (2, 0, 102), (3, 1, 103)], 2, 2)
        for worker in (-1, 2, True):
            self.invalid("trace-worker", core.contention,
                         [(0, worker, 100)] + good[1:], 2, 2)
        self.invalid("trace-sequence", core.contention,
                     good[:2] + [(1, 1, 102)] + good[3:], 2, 2)
        self.invalid("trace-timestamp", core.contention,
                     [(0, 0, True)] + good[1:], 2, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

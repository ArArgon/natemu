"""The page-study analysis script (tools/page_study_an.py) on a synthetic profile: sums vCPUs, models the gate."""
import contextlib
import io
import json
import pathlib
import tempfile
import unittest

import page_study_an as an


def vcpu(hit_reval, hit_place, walk_hit_ns, disagree=0):
    """One vCPU's `vcpu {...}` line: 1,000 revalidation and 400 placement walks, the record answering the given number."""
    page = {
        'calls': [1000, 400], 'hit_4k': [hit_reval, hit_place], 'hit_2m': [0, 0], 'class_calls': [[0, 0, 0]] * 2, 'class_hits': [[0, 0, 0]] * 2,
        'faulted': [0, 0], 'not_ram': [0, 0], 'disagree': [disagree, 0], 'disagree_frame': disagree, 'disagree_flags': 0, 'disagree_fault': 0,
        'records': 10, 'evictions': 1, 'split_2m': 2, 'clear_events': 3, 'cleared': 4, 'live_max': 7, 'probe_ns': 1000,
        'walk_ns': [[(1000 - hit_reval) * 200, hit_reval * walk_hit_ns], [(400 - hit_place) * 300, hit_place * walk_hit_ns]],
        'walk_count': [[1000 - hit_reval, hit_reval], [400 - hit_place, hit_place]], 'blocks': [5, 1, 0, 2, 1], 'blocks_hit': [4, 1, 0, 1, 0],
        'tick_pair_milli_ns': 250,
    }
    buckets = {'revalidate': [250_000, 1000], 'fetch': [150_000, 400], 'guest': [1_000_000, 3], 'halted': [999, 1]}
    return 'vcpu ' + json.dumps({'k1': {'page_study': page}, 'resident': {'buckets': buckets}})


class Analysis(unittest.TestCase):
    def run_report(self, lines, step='configure'):
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / f'perf-{step}.log').write_text('\n'.join(lines) + '\n')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                an.report(pathlib.Path(d), step)
            return out.getvalue()

    def test_sums_the_vcpus_and_models_the_gate(self):
        text = self.run_report([vcpu(800, 200, 100), vcpu(800, 200, 100)])
        self.assertIn('2 vCPUs', text)
        self.assertIn('2,000 fetch walks', text)                      # revalidation: 2 x 1,000
        self.assertIn('would have answered 1,600 ( 80.0%', text)
        # saved: (1600 + 400) walks x 100 ns = 200 us; probes 2,800 x 15 ns = 42 us; stores (400 + 400) x 5 ns = 4 us
        self.assertIn('net 0.00 vCPU-s', text)
        self.assertIn('FAIL', text)                                    # far below the 1.5 s gate
        self.assertIn('most records made in one epoch 7', text)        # a maximum over the vCPUs, not a sum
        self.assertIn('disagreements 0 of 2,000', text)
        self.assertIn('PASS', text)

    def test_disagreements_fail_the_gate(self):
        one = self.run_report([vcpu(800, 200, 100, disagree=1)])
        self.assertIn('FAIL (trace the address', one)
        many = self.run_report([vcpu(800, 200, 100, disagree=1)] * 4)
        self.assertIn('= 1000.00 per million', many)                   # 4 of 4,000: not a small rate
        self.assertIn('FAIL (trace the address', many)

    def test_a_log_without_the_counters_says_so(self):
        plain = 'vcpu ' + json.dumps({'k1': {}, 'resident': {'buckets': {}}})
        self.assertIn('no page_study counters', self.run_report([plain]))


if __name__ == '__main__':
    unittest.main()

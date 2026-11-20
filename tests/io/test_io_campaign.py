"""run_io_campaign's argument assembly (no VM)."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import unittest
from pathlib import Path

import run_io_campaign as c

ROOT = Path('/root')


def plan(case, out='/o'):
    return c.plan(case, ROOT, Path('/b/release/natemu-host'), Path('/b/rt'), Path('/b/release/natemu'), Path('/b/fexc'), Path(out))


def has(argv, *seq):
    return any(argv[i:i + len(seq)] == list(seq) for i in range(len(argv)))


class IoCampaign(unittest.TestCase):
    def test_serial_loaded_uses_factor_3_and_cpu_load(self):
        prepare, run, _, _ = plan('serial-loaded')
        self.assertTrue(has(prepare, '--factor', '3', '--cpu-load', '--stress', '/root/io-safety-stress-20261108-01/bin/stress-ng'))
        self.assertIn('--stress-provenance', prepare)
        self.assertTrue(has(run, '--mode', 'unpaced'))

    def test_serial_unpaced_and_paced_run_factor_1_without_load(self):
        for case, mode in (('serial-unpaced', 'unpaced'), ('serial-paced', 'paced')):
            prepare, run, src, clone = plan(case)
            self.assertNotIn('--cpu-load', prepare); self.assertNotIn('--factor', prepare); self.assertIsNone(clone)
            self.assertTrue(has(prepare, '--matrix-inputs', '/root/io-matrix-inputs-20261118-01'))
            self.assertTrue(has(run, '--mode', mode))

    def test_soak_prepares_on_a_writable_clone_inside_out(self):
        prepare, run, src, clone = plan('soak', '/o')
        self.assertEqual((src, clone), (ROOT / c.MATRIX, Path('/o/matrix-clone')))
        self.assertTrue(has(prepare, '--matrix-inputs', '/o/matrix-clone'))
        self.assertIn('--stress-provenance', prepare)
        self.assertTrue(has(run, '--backend', 'natemu', '--executable', '/b/release/natemu'))
        self.assertTrue(has(run, '--build-manifest', '/o/build-manifest.json'))


if __name__ == '__main__': unittest.main()

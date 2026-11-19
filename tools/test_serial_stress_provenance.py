"""run_serial_stress prepare --cpu-load's stress-ng pin with --stress-provenance (the framework's build,
tools/io/stress-ng): the binary must hash to the provenance file's binary_sha256 and the source to the unchanged
STRESS_SOURCE_SHA."""
import argparse
import hashlib
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import run_serial_stress


def fixture(d: pathlib.Path, binary_sha=None):
    (d / 'bin').mkdir()
    stress, source, prov = d / 'bin/stress-ng', d / 'source.tar.xz', d / 'provenance.json'
    stress.write_bytes(b'\x7fELF fake stress-ng'); source.write_bytes(b'fake orig tarball')
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    prov.write_text(json.dumps({'binary_sha256': binary_sha or sha(stress), 'source_sha256': sha(source),
                                'recipe_sha256': '0' * 64, 'builder': 'test'}))
    return argparse.Namespace(stress=stress, stress_source=source, stress_provenance=prov, factor=3, cpu_load=True), sha(source)


class SerialStressProvenance(unittest.TestCase):
    def test_serial_stress_accepts_matching_provenance(self):
        with tempfile.TemporaryDirectory() as t:
            args, source_sha = fixture(pathlib.Path(t))
            with mock.patch.object(run_serial_stress, 'STRESS_SOURCE_SHA', source_sha):
                record = run_serial_stress.stress_pin(args)
            self.assertEqual(record['binary_sha256'], hashlib.sha256(args.stress.read_bytes()).hexdigest())
            self.assertEqual(record['source_sha256'], source_sha)
            self.assertEqual(record['provenance']['content']['builder'], 'test')

    def test_serial_stress_refuses_mismatched_provenance(self):
        with tempfile.TemporaryDirectory() as t:
            args, source_sha = fixture(pathlib.Path(t), binary_sha='1' * 64)
            with mock.patch.object(run_serial_stress, 'STRESS_SOURCE_SHA', source_sha), self.assertRaises(ValueError) as e:
                run_serial_stress.stress_pin(args)
            self.assertEqual(str(e.exception), 'stress binary/source pin')

    def test_serial_stress_refuses_other_source_with_provenance(self):
        with tempfile.TemporaryDirectory() as t:
            args, _ = fixture(pathlib.Path(t))
            with self.assertRaises(ValueError) as e:
                run_serial_stress.stress_pin(args)
            self.assertEqual(str(e.exception), 'stress binary/source pin')

    def test_serial_stress_without_provenance_keeps_the_retained_pin(self):
        with tempfile.TemporaryDirectory() as t:
            args, source_sha = fixture(pathlib.Path(t))
            args.stress_provenance = None
            with mock.patch.object(run_serial_stress, 'STRESS_SOURCE_SHA', source_sha), self.assertRaises(ValueError) as e:
                run_serial_stress.stress_pin(args)
            self.assertEqual(str(e.exception), 'pinned stress binary/source mismatch')    # the old message, unchanged


if __name__ == '__main__': unittest.main()

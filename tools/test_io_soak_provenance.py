"""run_io_soak prepare's stress-ng pin with --stress-provenance (the framework's build, tools/io/stress-ng): the binary must
hash to the provenance file's binary_sha256 and the source to the unchanged SOURCE_SHA."""
import argparse
import hashlib
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import run_io_soak


def fixture(d: pathlib.Path, binary_sha=None):
    """A stress binary, a source tarball and a provenance file; SOURCE_SHA is patched to the fake source's hash."""
    (d / 'bin').mkdir()
    stress, source, prov = d / 'bin/stress-ng', d / 'source.tar.xz', d / 'provenance.json'
    stress.write_bytes(b'\x7fELF fake stress-ng'); source.write_bytes(b'fake orig tarball')
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    prov.write_text(json.dumps({'binary_sha256': binary_sha or sha(stress), 'source_sha256': sha(source),
                                'recipe_sha256': '0' * 64, 'builder': 'test'}))
    return argparse.Namespace(stress=stress, stress_source=source, stress_provenance=prov), sha(source)


class SoakProvenance(unittest.TestCase):
    def test_soak_accepts_matching_provenance(self):
        with tempfile.TemporaryDirectory() as t:
            args, source_sha = fixture(pathlib.Path(t))
            with mock.patch.object(run_io_soak, 'SOURCE_SHA', source_sha):
                record = run_io_soak.stress_pin(args)
            self.assertEqual(record['sha256'], hashlib.sha256(args.stress.read_bytes()).hexdigest())
            self.assertEqual(record['source_sha256'], source_sha)
            self.assertEqual(record['provenance']['builder'], 'test')        # recorded in the manifest
            self.assertNotIn('prior_binary_manifest', record)                # no retained build beside the binary

    def test_soak_refuses_mismatched_provenance(self):
        with tempfile.TemporaryDirectory() as t:
            args, source_sha = fixture(pathlib.Path(t), binary_sha='1' * 64)
            with mock.patch.object(run_io_soak, 'SOURCE_SHA', source_sha), self.assertRaises(ValueError) as e:
                run_io_soak.stress_pin(args)
            self.assertEqual(str(e.exception), 'stress binary/source pin')

    def test_soak_refuses_other_source_with_provenance(self):
        with tempfile.TemporaryDirectory() as t:
            args, _ = fixture(pathlib.Path(t))                                # SOURCE_SHA left as pinned
            with self.assertRaises(ValueError) as e:
                run_io_soak.stress_pin(args)
            self.assertEqual(str(e.exception), 'stress binary/source pin')

    def test_soak_without_provenance_keeps_the_retained_pin(self):
        with tempfile.TemporaryDirectory() as t:
            args, source_sha = fixture(pathlib.Path(t))
            args.stress_provenance = None
            with mock.patch.object(run_io_soak, 'SOURCE_SHA', source_sha), self.assertRaises(ValueError) as e:
                run_io_soak.stress_pin(args)                                  # not STRESS_SHA: refused as before
            self.assertEqual(str(e.exception), 'stress binary/source pin')


if __name__ == '__main__': unittest.main()

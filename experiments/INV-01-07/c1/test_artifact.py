"""Author-side, synthetic adapter checks. No host probe or evidence artifact.

Temporary bytes exercise parser paths only; independent fixture coverage and
full artifact/CLI acceptance remain separate code-gate obligations.
"""
import hashlib
import contextlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import artifact
import check
import core


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="natemu-c1-parser-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def test_binary_layout_retains_ids_and_uint64(self):
        payload = struct.pack("<QQQQQQ", 0, 17, 19, 1, 23, core.U64_MAX)
        rows = artifact.BinaryRows(payload, 3)
        self.assertEqual(len(rows), 2)
        self.assertEqual(list(rows), [(0, 17, 19), (1, 23, core.U64_MAX)])
        with self.assertRaisesRegex(core.Invalid, "array-partial-row"):
            artifact.BinaryRows(payload[:-1], 3)

    def test_descriptor_fields_paths_domains_and_encoding(self):
        good = {"path": "clock-0.bin", "sha256": "a" * 64,
                "records": 200001, "columns": 2, "encoding": "little-endian-uint64"}
        self.assertEqual(artifact.descriptor(good, 200001, 2), 3200016)
        for key, value in (("path", "../x.bin"), ("path", "/x.bin"),
                           ("path", "a/b.bin"), ("sha256", "A" * 64),
                           ("records", True), ("records", -1), ("records", 200000),
                           ("columns", 2.0), ("encoding", "native-endian")):
            with self.subTest(key=key, value=value), self.assertRaises(core.Invalid):
                artifact.descriptor({**good, key: value}, 200001, 2)
        for mutation in ({**good, "extra": 1}, {k: v for k, v in good.items() if k != "path"}):
            with self.assertRaises(core.Invalid):
                artifact.descriptor(mutation, 200001, 2)

    def test_json_rejects_duplicate_keys_nonfinite_and_invalid_utf8(self):
        path = self.root / "synthetic.json"
        for content in (b'{"seed":70701,"seed":70702}', b'{"x":NaN}',
                        b'{"x":Infinity}', b'{"x":-Infinity}', b'\xff', b'{'):
            path.write_bytes(content)
            with self.subTest(content=content), self.assertRaises(core.Invalid):
                artifact.read_json(path)
        path.write_text('{"x":18446744073709551615}', encoding="utf-8")
        self.assertEqual(artifact.read_json(path), {"x": core.U64_MAX})

    def test_read_regular_exact_bytes_hash_size_and_symlink(self):
        path = self.root / "array.bin"
        payload = b"synthetic bytes, not clock data"
        path.write_bytes(payload)
        actual = artifact.read_regular(path, len(payload), exact_size=len(payload))
        self.assertEqual(actual, payload)
        self.assertEqual(hashlib.sha256(actual).digest(), hashlib.sha256(payload).digest())
        with self.assertRaisesRegex(core.Invalid, "array-byte-size"):
            artifact.read_regular(path, len(payload), exact_size=len(payload) - 1)
        with self.assertRaisesRegex(core.Invalid, "artifact-size-cap"):
            artifact.read_regular(path, len(payload) - 1)
        alias = self.root / "alias.bin"
        alias.symlink_to(path)
        with self.assertRaises(OSError):
            artifact.read_regular(alias, 100)
        with self.assertRaises((core.Invalid, OSError)):
            artifact.read_regular(self.root, 100)

    def test_canonical_directory_rejects_alias_and_relative(self):
        actual = self.root / "actual"
        actual.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(actual, target_is_directory=True)
        self.assertEqual(artifact.canonical_directory(str(actual), "test"), actual)
        for value in (str(alias), str(actual) + "/../actual", "relative", str(actual) + "/"):
            with self.subTest(value=value), self.assertRaises(core.Invalid):
                artifact.canonical_directory(value, "test")

    def test_location_rejects_internal_or_unresolved_repository(self):
        # Uses the real read-only repository identity, never a fabricated review.
        repository = Path(__file__).resolve().parents[3]
        child = self.root / "child"
        child.mkdir()
        self.assertEqual(artifact.validate_locations(str(child), str(self.root),
                                                      str(repository)), child)
        with self.assertRaisesRegex(core.Invalid, "repository-internal-root"):
            artifact.validate_locations(str(repository / "experiments"),
                                        str(repository), str(repository))
        unknown_repository = self.root / "unknown-repository"
        unknown_repository.mkdir()
        with self.assertRaisesRegex(core.Invalid, "unresolved-repository"):
            artifact.validate_locations(str(child), str(self.root), str(unknown_repository))

    def test_fixed_payload_matches_documented_budget(self):
        # Literal independent column/count accounting, no producer helper.
        size = (4 * 200001 * 16 + 8 * 200001 * 40 + 12 * 200 * 24
                + 24 * 10000 * 24 + 24 * 256 * 56 + 6000000 * 24)
        self.assertEqual(size, 226962048)
        self.assertLess(size, artifact.PAYLOAD_LIMIT)

    def test_load_rejects_incomplete_matrix_before_reading_arrays(self):
        repository = Path(__file__).resolve().parents[3]
        child = self.root / "incomplete"
        child.mkdir()
        record = {"schema": "INV-01-07-clock-c1-v1", "seed": 70701,
                  "mode": "normal", "timebase": {"status": 0, "numer": 1, "denom": 1},
                  "clock_cells": [], "contention_cells": []}
        (child / "record.json").write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(core.Invalid, "clock-matrix-count"):
            artifact.load_child(str(child), str(self.root), str(repository))

    def test_cli_missing_root_emits_only_error_transaction(self):
        output = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(output):
            code = check.main([str(self.root / "absent")])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue()),
                         {"status": "INVALID", "errors": ["missing-NATEMU_RAW_ROOT"]})

    def test_cli_malformed_metadata_has_no_findings_prefix(self):
        child = self.root / "malformed"
        child.mkdir()
        (child / "record.json").write_text('{"schema": "x", "schema": "y"}', encoding="utf-8")
        output = io.StringIO()
        with patch.dict("os.environ", {"NATEMU_RAW_ROOT": str(self.root)}), contextlib.redirect_stdout(output):
            code = check.main([str(child)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue()),
                         {"status": "INVALID", "errors": ["duplicate-json-key"]})

    def test_report_fraction_encoding_never_rounds(self):
        from fractions import Fraction
        self.assertEqual(json.loads(json.dumps({"cost": Fraction(100000, 1001)}, default=check.encode)),
                         {"cost": {"numerator": 100000, "denominator": 1001}})


if __name__ == "__main__":
    unittest.main(verbosity=2)

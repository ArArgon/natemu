"""Author-side complete binary/CLI round trip using independent synthetic rows.

Writes 226962048 bytes in a new external temporary directory, never evidence.
The row oracle comes from test_cells_independent, not production summaries.
No host timing/native code runs. Independent code review remains required.
"""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from test_cells_independent import Rows, child_record


def materialize(value, directory, files):
    if isinstance(value, Rows):
        width = len(value[0])
        layout = struct.Struct("<" + "Q" * width)
        name = "synthetic-%03d.bin" % len(files)
        digest = hashlib.sha256()
        buffer = bytearray()
        with (directory / name).open("xb") as stream:
            for row in value:
                buffer.extend(layout.pack(*row))
                if len(buffer) >= 65536:
                    stream.write(buffer)
                    digest.update(buffer)
                    buffer.clear()
            stream.write(buffer)
            digest.update(buffer)
        files.append(name)
        return {"path": name, "sha256": digest.hexdigest(), "records": len(value),
                "columns": width, "encoding": "little-endian-uint64"}
    if type(value) is dict:
        return {key: materialize(item, directory, files) for key, item in value.items()}
    if type(value) is list:
        return [materialize(item, directory, files) for item in value]
    return value


class BinaryIntegration(unittest.TestCase):
    def test_full_binary_and_real_cli_late_failure(self):
        with tempfile.TemporaryDirectory(prefix="natemu-c1-binary-test-") as temporary:
            root = Path(temporary).resolve()
            child = root / "synthetic-child"
            child.mkdir()
            fixture = child_record()
            process = fixture.pop("terminal")
            files = []
            metadata = materialize(fixture, child, files)
            self.assertEqual(len(files), 80)
            self.assertEqual(sum((child / name).stat().st_size for name in files), 226962048)
            (child / "record.json").write_text(json.dumps(metadata), encoding="utf-8")
            (child / "process.json").write_text(json.dumps(process), encoding="utf-8")
            command = [sys.executable, "-B", str(Path(__file__).with_name("check.py")), str(child)]
            environment = {**os.environ, "NATEMU_RAW_ROOT": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
            # Development-only operational timeout, not the future native
            # evidence campaign's prescribed 300s child watchdog.
            normal = subprocess.run(command, env=environment, capture_output=True,
                                    text=True, timeout=120, check=False)
            self.assertEqual(normal.returncode, 0, normal.stdout + normal.stderr)
            self.assertEqual(normal.stderr, "")
            report = json.loads(normal.stdout)
            self.assertEqual(report["status"], "STRUCTURAL_PASS")
            self.assertIs(report["evidence_approval"], False)
            self.assertEqual(len(report["array_sha256"]), 80)
            for result in report["findings"]["T1"].values():
                self.assertEqual(result, {"best_p50_ns": {"numerator": 100, "denominator": 1},
                                          "strict_100ns_meet": False})
            # Launch a separate Python process through the same main/real
            # artifact path, injecting failure only when an exact Fraction
            # reaches the final JSON encoder after every required cell passed.
            for error_type, label in (("RuntimeError", "runtime"), ("OverflowError", "overflow")):
                script = (
                    "import sys; sys.path.insert(0, sys.argv[1]); import check\n"
                    "def broken(value):\n"
                    f"    raise {error_type}('partial: STRUCTURAL_PASS findings T1 strict_100ns_meet=true')\n"
                    "check.encode = broken\n"
                    "raise SystemExit(check.main([sys.argv[2]]))\n"
                )
                rendering = subprocess.run(
                    [sys.executable, "-B", "-c", script, str(Path(__file__).parent), str(child)],
                    env=environment, capture_output=True, text=True, timeout=120, check=False)
                self.assertEqual(rendering.returncode, 1, rendering.stdout + rendering.stderr)
                self.assertEqual(rendering.stderr, "")
                self.assertEqual(json.loads(rendering.stdout),
                                 {"status": "INVALID", "errors": [f"unexpected-{label}-error"]})
                self.assertNotIn("findings", rendering.stdout)
                self.assertNotIn("T1", rendering.stdout)
                self.assertNotIn("PASS", rendering.stdout)
            # Only the final required contention summary is corrupted: every
            # array remains complete/hash-correct, so this reaches a genuinely
            # late validator rejection after preceding cells were evaluated.
            metadata["contention_cells"][-1]["global_scan"]["forward"] -= 1
            (child / "record.json").write_text(json.dumps(metadata), encoding="utf-8")
            failed = subprocess.run(command, env=environment, capture_output=True,
                                    text=True, timeout=120, check=False)
            self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
            self.assertEqual(failed.stderr, "")
            self.assertEqual(json.loads(failed.stdout),
                             {"status": "INVALID", "errors": ["scan-summary-mismatch"]})
            self.assertNotIn("findings", failed.stdout)
            self.assertNotIn("T1", failed.stdout)
            self.assertNotIn("PASS", failed.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

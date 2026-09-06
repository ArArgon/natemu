"""Small synthetic v2 campaign; no native producer or clock sampling."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import clock_evidence as evidence


class ClockEvidenceTests(unittest.TestCase):
    def test_malformed_clock_metadata_is_unqualified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = root / "bad.json"
            with patch.dict(os.environ, {"NATEMU_RAW_ROOT": str(root)}):
                for value in (b"[]", b"null", b"true", b"123", b"{}", b"\xff",
                              b"[" * 2000 + b"]" * 2000,
                              b'{"schema": "a", "schema": "b"}'):
                    path.write_bytes(value)
                    self.assertEqual(evidence.validate(path, {"host_model": "synthetic"})["status"],
                                     "unqualified")

    def test_absent_historical_context_controls_and_retained_raw(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"NATEMU_RAW_ROOT": tmp}):
            root = Path(tmp).resolve()
            os.environ["NATEMU_RAW_ROOT"] = str(root)
            directory = root / "campaign"
            directory.mkdir()
            build_dir = root / "build"
            build_dir.mkdir()
            build = {"schema": "INV-01-07-clock-c1-build-v2", "build_id": "b" * 32,
                     "files": {}, "commands": [], "sdk": "synthetic", "compiler": "synthetic",
                     "headers": {}, "validation_exit": 0, "native_executed": False}
            for name in ("clock_c1", "sources/producer.c", "sources/runner.py", "sources/artifact.py",
                         "sources/core.py", "sources/cells.py", "sources/check.py", "sources/clock_evidence.py",
                         "sources/source-context.json", "disassembly.stdout", "validation.stdout", "validation.stderr"):
                target = build_dir / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"synthetic")
                build["files"][name] = evidence.sha(target)
            (build_dir / "build.json").write_text(json.dumps(build))
            record = {"schema": evidence.SCHEMA, "run_id": "a" * 32, "build_id": "b" * 32,
                      "status": "COMPLETE_STRUCTURAL_CAMPAIGN", "hashing_complete": True,
                      "context": {"host_model": "synthetic"}, "children": [], "sha256": {}}
            def put(name, value):
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                data = value if isinstance(value, bytes) else json.dumps(value).encode()
                path.write_bytes(data)
                record["sha256"][name] = hashlib.sha256(data).hexdigest()
            put("plan.json", {key: record[key] for key in ("schema", "run_id", "build_id")} |
                {"build_directory": str(build_dir), "build": build})
            put("environment-before.json", {})
            for index, (seed, mode) in enumerate(evidence.SCHEDULE):
                identity = {"index": index, "seed": seed, "mode": mode,
                            "run_id": record["run_id"], "build_id": record["build_id"]}
                status = "STRUCTURAL_PASS" if mode == "normal" else "EXPECTED_CONTROL_FAILURE"
                process = {"exit_status": 2 if mode in ("zero-nsec", "pthread-error") else 0,
                           "signal": None, "timeout": False, "complete": mode not in ("zero-nsec", "pthread-error")}
                record["children"].append(identity | {"status": status, "process": process})
                stem = f"{index:02d}-{mode}-{seed}"
                for name, value in (("identity.json", identity), ("process.json", process),
                                    ("check.json", {"status": status}), ("preflight.json", {})):
                    put(f"{stem}/{name}", value)
                if mode in ("zero-nsec", "pthread-error"):
                    put(f"{stem}/failure.json", {"mode": mode, "seed": seed,
                        "code": "nsec-zero" if mode == "zero-nsec" else "pthread-lock"})
                else:
                    arrays = []
                    for number in range(80):
                        name = f"{number}.bin"
                        put(f"{stem}/{name}", b"\0" * 8)
                        arrays.append({"path": name, "encoding": "little-endian-uint64", "records": 1,
                                       "columns": 1, "sha256": record["sha256"][f"{stem}/{name}"]})
                    put(f"{stem}/record.json", {"mode": mode, "seed": seed, "arrays": arrays})
                    put(f"{stem}/resources.json", {})
                    put(f"{stem}/execution.json", {})
            path = directory / "campaign.json"
            def result():
                path.write_text(json.dumps(record))
                return evidence.validate(path, {"host_model": "synthetic"})
            self.assertEqual(result()["status"], "context-matched")
            saved_files = build["files"]
            build["files"] = {}
            (build_dir / "build.json").write_text(json.dumps(build))
            put("plan.json", {key: record[key] for key in ("schema", "run_id", "build_id")} |
                {"build_directory": str(build_dir), "build": build})
            self.assertIn("incomplete clock producing build", result()["reason"])
            build["files"] = saved_files
            (build_dir / "build.json").write_text(json.dumps(build))
            put("plan.json", {key: record[key] for key in ("schema", "run_id", "build_id")} |
                {"build_directory": str(build_dir), "build": build})

            self.assertEqual(evidence.validate(None, {})["status"], "unqualified")
            self.assertEqual(evidence.validate(path, {"host_model": "other"})["status"], "unqualified")
            record["schema"] = "INV-01-07-clock-c1-campaign-v1"
            self.assertIn("historical", result()["reason"])
            record["schema"] = evidence.SCHEMA
            item = record["sha256"].pop("00-normal-70701/0.bin")
            self.assertIn("raw array", result()["reason"])
            record["sha256"]["00-normal-70701/0.bin"] = item
            record["children"][-1]["status"] = "NOT_RUN"
            self.assertIn("control failed", result()["reason"])
            record["children"][-1]["status"] = "EXPECTED_CONTROL_FAILURE"
            record["children"][0]["run_id"] = "c" * 32
            self.assertIn("mixed clock child", result()["reason"])


if __name__ == "__main__":
    unittest.main()

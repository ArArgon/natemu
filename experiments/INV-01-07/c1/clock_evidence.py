"""Factual C1 v2 campaign validation; no review interpretation or approval.

A context match is only a candidate clock reference. Reviewers decide whether
its instrument, conditions and resolution apply to the consuming measurement.
This module is standalone so consumers can retain it with their source snapshot.
"""
import hashlib
import json
import os
from pathlib import Path

SCHEMA = "INV-01-07-clock-c1-campaign-v2"
SCHEDULE = [(seed, "normal") for seed in (70701, 70702, 70703)] + [
    (70701, mode) for mode in ("backward", "zero-nsec", "omit-clock", "omit-contention",
                             "pthread-error", "global-inversion")]


def read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("invalid clock metadata file")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate clock metadata key")
            result[key] = value
        return result
    value = json.loads(path.read_bytes(), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("clock metadata must be an object")
    return value


def sha(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def validate(path, context):
    if path is None:
        return {"status": "unqualified", "reason": "clock evidence absent"}
    try:
        path = Path(path)
        root = Path(os.environ["NATEMU_RAW_ROOT"]).resolve(strict=True)
        if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_relative_to(root):
            raise ValueError("clock evidence outside canonical raw root")
        record = read(path)
        if record.get("schema") != SCHEMA:
            raise ValueError("historical or unknown clock format")
        if record.get("status") != "COMPLETE_STRUCTURAL_CAMPAIGN" or record.get("hashing_complete") is not True:
            raise ValueError("incomplete clock campaign")
        if (not isinstance(context, dict) or not context or not isinstance(record.get("context"), dict)
                or any(record["context"].get(key) != value for key, value in context.items())):
            raise ValueError("clock context mismatch")
        directory = path.parent
        inventory = record["sha256"]
        if not isinstance(inventory, dict) or not {"plan.json", "environment-before.json"}.issubset(inventory):
            raise ValueError("missing clock inventory")
        for name, expected in inventory.items():
            item = directory / name
            if Path(name).is_absolute() or ".." in Path(name).parts or item.resolve(strict=True) != item:
                raise ValueError("invalid clock artifact path")
            if not item.is_file() or item.stat().st_size > 512 * 1024 * 1024 or sha(item) != expected:
                raise ValueError("clock artifact checksum mismatch")
        plan = read(directory / "plan.json")
        if any(plan.get(key) != record.get(key) for key in ("schema", "run_id", "build_id")):
            raise ValueError("mixed clock plan")
        build_directory = Path(plan["build_directory"])
        if (not build_directory.is_absolute() or build_directory.resolve(strict=True) != build_directory
                or not build_directory.is_relative_to(root)):
            raise ValueError("clock build outside raw root")
        build = read(build_directory / "build.json")
        if (build != plan["build"] or build.get("schema") != "INV-01-07-clock-c1-build-v2"
                or build.get("build_id") != record["build_id"]):
            raise ValueError("mixed clock build")
        required_build = {"clock_c1", "sources/producer.c", "sources/runner.py", "sources/artifact.py",
                          "sources/core.py", "sources/cells.py", "sources/check.py", "sources/clock_evidence.py",
                          "sources/source-context.json", "disassembly.stdout", "validation.stdout", "validation.stderr"}
        if (set(build) != {"schema", "build_id", "files", "commands", "sdk", "compiler", "headers",
                           "validation_exit", "native_executed"}
                or type(build["validation_exit"]) is not int or build["validation_exit"] != 0
                or build["native_executed"] is not False or not isinstance(build["files"], dict)
                or not required_build.issubset(build["files"])):
            raise ValueError("incomplete clock producing build")
        for name, expected in build["files"].items():
            item = build_directory / name
            if (Path(name).is_absolute() or ".." in Path(name).parts
                    or item.resolve(strict=True) != item or not item.is_file()
                    or sha(item) != expected):
                raise ValueError("clock build checksum mismatch")
        rows = record["children"]
        if not isinstance(rows, list) or len(rows) != len(SCHEDULE) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("missing clock controls")
        for index, ((seed, mode), row) in enumerate(zip(SCHEDULE, rows)):
            expected = {"index": index, "seed": seed, "mode": mode,
                        "run_id": record["run_id"], "build_id": record["build_id"]}
            if any(row.get(key) != value for key, value in expected.items()):
                raise ValueError("mixed clock child")
            status = "STRUCTURAL_PASS" if mode == "normal" else "EXPECTED_CONTROL_FAILURE"
            stem = f"{index:02d}-{mode}-{seed}"
            if row.get("status") != status:
                raise ValueError("clock correctness or control failed")
            for name in (f"{stem}/identity.json", f"{stem}/check.json", f"{stem}/process.json"):
                if name not in inventory:
                    raise ValueError("unretained clock child")
            if read(directory / stem / "identity.json") != expected:
                raise ValueError("mixed clock identity")
            if read(directory / stem / "check.json").get("status") != status:
                raise ValueError("clock check disagrees")
            expected_process = {"exit_status": 2 if mode in ("zero-nsec", "pthread-error") else 0,
                                "signal": None, "timeout": False,
                                "complete": mode not in ("zero-nsec", "pthread-error")}
            if read(directory / stem / "process.json") != expected_process or row.get("process") != expected_process:
                raise ValueError("clock process did not complete as expected")
            required = ["preflight.json"] + (["failure.json"] if mode in ("zero-nsec", "pthread-error")
                        else ["record.json", "resources.json", "execution.json"])
            if any(f"{stem}/{name}" not in inventory for name in required):
                raise ValueError("incomplete retained clock artifacts")
            if mode in ("zero-nsec", "pthread-error"):
                failure = read(directory / stem / "failure.json")
                expected_code = "nsec-zero" if mode == "zero-nsec" else "pthread-lock"
                if failure.get("code") != expected_code or failure.get("mode") != mode or failure.get("seed") != seed:
                    raise ValueError("wrong clock failure control")
            else:
                producer = read(directory / stem / "record.json")
                if producer.get("mode") != mode or producer.get("seed") != seed:
                    raise ValueError("mixed clock raw record")
                pending = [producer]
                arrays = 0
                while pending:
                    value = pending.pop()
                    if isinstance(value, list):
                        pending.extend(value)
                    elif isinstance(value, dict):
                        if value.get("encoding") == "little-endian-uint64":
                            name = value["path"]
                            if Path(name).name != name or inventory.get(f"{stem}/{name}") != value["sha256"]:
                                raise ValueError("missing or mixed clock raw array")
                            if (directory / stem / name).stat().st_size != value["records"] * value["columns"] * 8:
                                raise ValueError("clock raw array size mismatch")
                            arrays += 1
                        else:
                            pending.extend(value.values())
                if arrays != 80:
                    raise ValueError("incomplete clock raw array matrix")

        return {"status": "context-matched", "reason": "factual match; applicability requires findings review",
                "path": str(path), "sha256": sha(path), "run_id": record["run_id"],
                "build_id": record["build_id"]}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        return {"status": "unqualified", "reason": str(error)}

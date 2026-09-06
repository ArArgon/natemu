"""Read-only C1 binary-array adapter. Not an evidence runner or gate approval.

The native producer will emit record.json without a terminal claim. A separate
supervisor must record its actual wait/timeout result in process.json. Both
files and every array still need externally manifested integrity/provenance.
This module only verifies the structural relationship and array bytes.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct

import cells
import core

PAYLOAD_LIMIT = 512 * 1024 * 1024
METADATA_LIMIT = 1024 * 1024
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}\.bin\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def repository():
    context = Path(__file__).resolve().parent / "source-context.json"
    return (Path(json.loads(context.read_text())["repository"]) if context.exists()
            else Path(__file__).resolve().parents[3])


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        core.require(key not in result, "duplicate-json-key")
        result[key] = value
    return result


def reject_constant(value):
    raise core.Invalid("nonfinite-json-constant:" + value)


def canonical_directory(value, code):
    path = Path(value)
    core.require(path.is_absolute() and str(path) == str(value), code + "-absolute")
    core.require(path.resolve(strict=True) == path and path.is_dir(), code + "-canonical")
    return path


def validate_locations(directory, external_root, repository):
    root = canonical_directory(external_root, "raw-root")
    repo = Path(repository)
    core.require(repo.is_absolute() and repo.resolve(strict=False) == repo, "repository-canonical")
    child = canonical_directory(directory, "child-directory")
    core.require(root != repo and not root.is_relative_to(repo), "repository-internal-root")
    core.require(child != root and child.is_relative_to(root)
                 and not child.is_relative_to(repo), "child-outside-external-root")
    return child


def read_regular(path, limit, exact_size=None):
    """No symlink following; hash and parsing later use these same read bytes."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        core.require(stat.S_ISREG(before.st_mode), "artifact-not-regular")
        core.require(0 <= before.st_size <= limit, "artifact-size-cap")
        if exact_size is not None:
            core.require(before.st_size == exact_size, "array-byte-size")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    core.require(len(data) == before.st_size and len(data) <= limit,
                 "artifact-read-size")
    core.require((before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                 == (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                 "artifact-changed-during-read")
    return data


def read_json(path):
    data = read_regular(path, METADATA_LIMIT)
    try:
        return json.loads(data, object_pairs_hook=object_pairs,
                          parse_constant=reject_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise core.Invalid("malformed-json") from error


class BinaryRows:
    """Fixed little-endian uint64 rows; row IDs are stored, never inferred."""
    def __init__(self, payload, columns):
        self.payload = payload
        self.layout = struct.Struct("<" + "Q" * columns)
        core.require(len(payload) % self.layout.size == 0, "array-partial-row")

    def __len__(self):
        return len(self.payload) // self.layout.size

    def __iter__(self):
        return self.layout.iter_unpack(self.payload)


def descriptor(value, count, columns):
    cells.fields(value, ("path", "sha256", "records", "columns", "encoding"),
                 "array-descriptor-fields")
    core.require(type(value["path"]) is str and NAME.fullmatch(value["path"]),
                 "array-path")
    core.require(type(value["sha256"]) is str and DIGEST.fullmatch(value["sha256"]),
                 "array-digest")
    core.require(core.uint(value["records"], "array-records") == count
                 and core.uint(value["columns"], "array-columns") == columns,
                 "array-shape")
    core.require(value["encoding"] == "little-endian-uint64", "array-encoding")
    return count * columns * 8


def load_child(directory, external_root, repository):
    """Read no more than the predeclared full matrix; return records and hashes.

    Metadata identities and process records are NOT independent evidence of
    authorization, execution, or host applicability. The future runner and
    manifest/review chain must establish those separately.
    """
    child = validate_locations(directory, external_root, repository)
    record = read_json(child / "record.json")
    cells.fields(record, ("schema", "seed", "mode", "timebase",
                          "clock_cells", "contention_cells"), "producer-record-fields")
    cells.matrix(record["clock_cells"], core.QOS, core.CLOCKS, "clock-matrix")
    cells.matrix(record["contention_cells"], core.QOS, cells.WORKERS, "contention-matrix")
    pending = []
    for cell in record["clock_cells"]:
        core.require(all(key in cell for key in ("readings", "batches", "pre", "post")),
                     "missing-clock-array")
        pending.append((cell, "readings", core.READS,
                        2 if cell["clock"] in core.CLOCKS[:2] else 5))
        pending.append((cell, "batches", core.BATCHES, 3))
        for name in ("pre", "post"):
            phase = cells.fields(cell[name], ("noise", "containment"), "phase-fields")
            pending.extend(((phase, "noise", core.PAIRS, 3),
                            (phase, "containment", core.CONTROLS, 7)))
    for cell in record["contention_cells"]:
        core.require("trace" in cell, "missing-contention-array")
        pending.append((cell, "trace", cell["workers"] * core.WORKER_READS, 3))
    total, names = 0, set()
    for owner, key, count, columns in pending:
        item = owner[key]
        total += descriptor(item, count, columns)
        core.require(item["path"] not in names, "aliased-array-path")
        names.add(item["path"])
    core.require(total <= PAYLOAD_LIMIT, "child-payload-cap")
    hashes = {}
    # All descriptors/cap checks precede any large payload allocation.
    for owner, key, count, columns in pending:
        item = owner[key]
        data = read_regular(child / item["path"], count * columns * 8,
                            exact_size=count * columns * 8)
        digest = hashlib.sha256(data).hexdigest()
        core.require(digest == item["sha256"], "array-hash-mismatch")
        hashes[item["path"]] = digest
        owner[key] = BinaryRows(data, columns)
    record["terminal"] = read_json(child / "process.json")
    return record, hashes

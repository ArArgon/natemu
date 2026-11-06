#!/usr/bin/env python3
"""Static artifact check only; does not build FEX or execute generated code."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

here = Path(__file__).resolve().parent
if len(sys.argv) != 2:
    raise SystemExit(f"usage: {sys.argv[0]} PINNED_FEX_SOURCE_ROOT")
root = Path(sys.argv[1]).resolve(strict=True)
for manifest_name in ("source-manifest.json", "root-source-manifest.json"):
    manifest = json.loads((here / manifest_name).read_text())
    for name, expected in manifest["targets"].items():
        data = (root / name).read_bytes()
        assert len(data) == expected["bytes"], name
        assert hashlib.sha256(data).hexdigest() == expected["sha256"], name
fixtures = json.loads((here / "fixtures.json").read_text())
assert len(fixtures["Instructions"]) == 6
for name, item in fixtures["Instructions"].items():
    live = name.startswith("live_")
    assert len(item["x86Insts"]) == (5 if live else 1), name
    assert item["FaultInstructionIndex"] == (2 if live else 0), name
    assert len(bytes.fromhex(item["ExpectedBytes"])) == (17 if live else 3), name
one = json.loads((here / "root-fixture.json").read_text())
assert one == {"Features": fixtures["Features"], "Instructions": {"live_load": fixtures["Instructions"]["live_load"]}}
oracle = json.loads((here / "expected-recovery.json").read_text())
assert oracle["live_roots"] == {"s": 64, "b": 64, "old_RAX": 64, "XMM0_after_PXOR": 128}
for patch in ("fex-observation.patch", "fex-root-preparation.patch"):
    subprocess.run(["git", "apply", "--check", str(here / patch)], cwd=root, check=True)
print("PASS: pinned target hashes, fixture shape, requirements and patch applicability only; unbuilt")

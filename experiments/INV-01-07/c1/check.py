#!/usr/bin/env python3
"""C1 structural-check CLI, not an evidence-collection or approval command."""
import argparse
from fractions import Fraction
import json
import os
from pathlib import Path
import sys

import artifact
import cells
import core


def encode(value):
    if isinstance(value, Fraction):
        return {"numerator": value.numerator, "denominator": value.denominator}
    raise TypeError("unsupported-report-type")


def trusted_code(error):
    # Validator codes are lowercase identifiers, not arbitrary exception prose
    # or external path contents. Never let an error smuggle findings text into
    # a transactional failure report, including late JSON-rendering failures.
    public_codes = {
        "missing-NATEMU_RAW_ROOT", "duplicate-json-key", "malformed-json",
        "array-shape", "array-hash-mismatch", "array-byte-size", "array-partial-row",
        "array-path", "aliased-array-path", "child-payload-cap", "artifact-size-cap",
        "artifact-read-size", "artifact-changed-during-read", "artifact-not-regular",
        "repository-internal-root", "child-outside-external-root", "unresolved-repository",
        "clock-backward", "global-trace-backward", "worker-trace-backward",
        "scan-summary-mismatch", "cost-summary-mismatch", "clock-read-conversion",
        "containment-violation", "containment-slack-cap", "control-too-short",
        "clock-no-forward-step", "contention-no-progress", "containment-all-wide",
        "batch-at-or-below-floor", "injection-not-detected", "injected-or-unknown-mode",
        "timebase-api", "timebase-numer", "timebase-denom", "child-terminal", "child-exit",
    }
    value = str(error)
    return value if value in public_codes else "validator-error"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("child_directory", help="canonical external child artifact directory")
    args = parser.parse_args(argv)
    try:
        raw_root = os.environ.get("NATEMU_RAW_ROOT")
        core.require(bool(raw_root), "missing-NATEMU_RAW_ROOT")
        repository = Path(__file__).resolve().parents[3]
        record, hashes = artifact.load_child(args.child_directory, raw_root, str(repository))
        findings = cells.validate_child(record)
        output = {"status": "STRUCTURAL_PASS", "evidence_approval": False,
                  "array_sha256": hashes, "findings": findings}
        # Serialize before writing anything: a late conversion error cannot
        # leave behind earlier threshold/PASS prefixes.
        serialized = json.dumps(output, default=encode, sort_keys=True, allow_nan=False)
    except core.Unresolved as error:
        print(json.dumps({"status": "UNRESOLVED", "errors": [trusted_code(error)]}))
        return 2
    except core.Invalid as error:
        print(json.dumps({"status": "INVALID", "errors": [trusted_code(error)]}))
        return 1
    except Exception as error:
        labels = {OSError: "os", ValueError: "value", TypeError: "type", KeyError: "key",
                  OverflowError: "overflow", RecursionError: "recursion", RuntimeError: "runtime"}
        print(json.dumps({"status": "INVALID", "errors": ["unexpected-" + labels.get(type(error), "input") + "-error"]}))
        return 1
    sys.stdout.write(serialized + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

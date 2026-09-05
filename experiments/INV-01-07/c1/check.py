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
        print(json.dumps({"status": "UNRESOLVED", "errors": [str(error)]}))
        return 2
    except (core.Invalid, OSError, ValueError, TypeError, KeyError,
            OverflowError, RecursionError) as error:
        print(json.dumps({"status": "INVALID", "errors": [str(error)]}))
        return 1
    sys.stdout.write(serialized + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

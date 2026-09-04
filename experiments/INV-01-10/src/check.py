#!/usr/bin/env python3
"""Validate one INV-01-10 tool-inventory JSON output. Standard library only.

Usage: check.py <path-to-tools-output.json>
Exit 0 (PASS) if every rule holds; exit 1 (FAIL) and list violations
otherwise. This is the code-gate validation tool: it must FAIL on
tools.sh's own --negative output, exercised by run.sh before any evidence
run and recorded as the negative-control result, not a claim.
"""
import json
import sys


def check(d):
    failures = []

    def require(cond, msg):
        if not cond:
            failures.append(msg)

    # Every "tools" entry: present implies a non-empty path and a
    # non-empty version; absent implies both are null. This is the rule
    # --negative trips (clang reported present with an empty version).
    for name, entry in d.get("tools", {}).items():
        if entry.get("present"):
            require(bool(entry.get("path")), f"tools.{name}.path must be non-empty when present")
            require(bool(entry.get("version")), f"tools.{name}.version must be non-empty when present")
        else:
            require(entry.get("path") is None, f"tools.{name}.path must be null when absent")
            require(entry.get("version") is None, f"tools.{name}.version must be null when absent")

    presence = d.get("presence", {})
    for name in ("xcode_select_path", "cmake", "ninja", "qemu_system_x86_64"):
        entry = presence.get(name, {})
        if entry.get("present"):
            require(bool(entry.get("path")), f"presence.{name}.path must be non-empty when present")
        else:
            require(entry.get("path") is None, f"presence.{name}.path must be null when absent")

    xctrace = presence.get("xctrace", {})
    if xctrace.get("present"):
        require(bool(xctrace.get("path")), "presence.xctrace.path must be non-empty when present")
    else:
        require(
            xctrace.get("functional_without_xcode") is not True,
            "presence.xctrace.functional_without_xcode cannot be true when xctrace is absent",
        )

    brew = presence.get("brew", {})
    if brew.get("present"):
        require(bool(brew.get("path")), "presence.brew.path must be non-empty when present")
        require(bool(brew.get("version")), "presence.brew.version must be non-empty when present")
    else:
        require(brew.get("path") is None, "presence.brew.path must be null when absent")
        require(brew.get("version") is None, "presence.brew.version must be null when absent")

    listing = d.get("sdk_directory_listing", [])
    require(isinstance(listing, list) and len(listing) > 0, "sdk_directory_listing must be non-empty")
    require(
        any(name.endswith(".sdk") for name in listing),
        "sdk_directory_listing must contain at least one *.sdk entry",
    )

    return failures


def main(argv):
    if len(argv) != 2:
        print("usage: check.py <tools-output.json>", file=sys.stderr)
        return 2
    with open(argv[1]) as f:
        d = json.load(f)
    failures = check(d)
    if failures:
        print(f"FAIL ({len(failures)} rule(s) violated):")
        for f_ in failures:
            print(f"  - {f_}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

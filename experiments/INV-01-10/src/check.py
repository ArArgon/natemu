#!/usr/bin/env python3
"""Validate one INV-01-10 tool-inventory JSON output. Standard library only.

Usage: check.py <path-to-tools-output.json>
Exit 0 (PASS) if every rule holds; exit 1 (FAIL) and list violations
otherwise; exit 3 if the input is not valid JSON at all (a distinct status
from "rules violated" — REV-054 C3). This is the code-gate validation tool:
it must FAIL on tools.sh's own --negative output, and on a run in which a
required build/test/debug tool (clang, python3, git, codesign) is absent —
KA-09 falsified on this cell — even though every field stays internally
consistent (REV-054 C1 / REV-053 M3).
"""
import json
import sys

REQUIRED_PRESENT = ("clang", "python3", "git", "codesign")


def check(d):
    failures = []

    def require(cond, msg):
        if not cond:
            failures.append(msg)

    negative = bool(d.get("negative_control"))

    # Every "tools" entry: present implies a non-empty path and (except
    # codesign, which has no --version option) a non-empty version; absent
    # implies path/version/provenance are all null.
    for name, entry in d.get("tools", {}).items():
        if entry.get("present"):
            require(bool(entry.get("path")), f"tools.{name}.path must be non-empty when present")
            require(bool(entry.get("provenance")), f"tools.{name}.provenance must be non-empty when present")
            if name == "codesign":
                require(
                    entry.get("version") is None and bool(entry.get("version_unavailable_reason")),
                    "tools.codesign.version must be null with a non-empty "
                    "version_unavailable_reason when present",
                )
            else:
                require(bool(entry.get("version")), f"tools.{name}.version must be non-empty when present")
        else:
            require(entry.get("path") is None, f"tools.{name}.path must be null when absent")
            require(entry.get("provenance") is None, f"tools.{name}.provenance must be null when absent")
            if name != "codesign":
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
        # A functional result must be backed by a captured message either
        # way: true needs no failure message, but the field must exist;
        # false should carry the captured failure text when the tool ran at
        # all (REV-054 C2 — the message must be verifiable from the
        # artifact, not merely asserted in prose).
        require(
            "message" in xctrace, "presence.xctrace.message must be present when xctrace is present"
        )
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

    require(bool(d.get("path")), "path must be non-empty")

    # --- KA-09-relevant REQUIRED outcomes (REV-054 C1 / REV-053 M3) ---
    # Internal consistency alone lets a run with a required tool absent
    # PASS. A non-negative run must also demonstrate the build/test/debug
    # tools KA-09 covers on this cell (profiling is a separate, open limb —
    # see INV-01-10.md Limitations, not enforced here).
    if not negative:
        tools = d.get("tools", {})
        for name in REQUIRED_PRESENT:
            require(
                tools.get(name, {}).get("present") is True,
                f"tools.{name}.present must be true (KA-09 build/test/debug limb)",
            )

    return failures


def main(argv):
    if len(argv) != 2:
        print("usage: check.py <tools-output.json>", file=sys.stderr)
        return 2
    try:
        with open(argv[1]) as f:
            d = json.load(f)
    except json.JSONDecodeError as e:
        print(f"MALFORMED INPUT (not valid JSON, not a rule violation): {e}")
        return 3
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

#!/usr/bin/env python3
"""Fault-injection matrix for the INV-01-07 containment checker."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def run_case(checker, payload, expect_pass, label):
    fd, path = tempfile.mkstemp(prefix="inv-01-07-control-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream)
        proc = subprocess.run(
            [sys.executable, str(checker), path],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    finally:
        os.unlink(path)
    passed = proc.returncode == 0
    if passed != expect_pass:
        print(f"control fixture {label}: unexpected return {proc.returncode}",
              file=sys.stderr)
        print(proc.stdout, file=sys.stderr)
        return False
    print(f"control fixture {label}: {'PASS' if expect_pass else 'rejected'}")
    return True


def arm(payload, target=100000, qos="user-interactive",
        mechanism="mach_wait_until"):
    return next(row for row in payload["wakeup"]
                if row["target_ns"] == target and row["qos"] == qos
                and row["mechanism"] == mechanism)


def changed(base, mutate):
    payload = copy.deepcopy(base)
    mutate(payload)
    return payload


def main():
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} CHECKER VALID_FIXTURE", file=sys.stderr)
        return 2
    checker, fixture = map(Path, sys.argv[1:])
    base = json.loads(fixture.read_text())
    cases = [(base, True, "baseline")]

    # A ±1% conversion defect must fail in both directions at every target.
    for target in (100000, 1000000, 10000000):
        for direction, field in (("plus", "timebase_control_max_lower_residual_ns"),
                                 ("minus", "timebase_control_max_upper_residual_ns")):
            def inject(payload, target=target, field=field):
                row = arm(payload, target=target)
                row["timebase_control_violation_count"] = 1
                row[field] = target // 100 - row["timebase_control_slack_ns"]
            cases.append((changed(base, inject), False,
                          f"conversion-{direction}-1pct-{target}"))

    # Each paired-read scheduling gap may widen one sample while another
    # sample keeps the arm non-vacuous. Producer arithmetic is checked by the
    # C self-test; these cases verify checker acceptance of the aggregates.
    for location in ("m0-c0", "c0-base", "woke-c1", "c1-m1"):
        def widen(payload):
            row = arm(payload)
            row["timebase_control_tight_count"] = row["n"] - 1
        cases.append((changed(base, widen), True, f"scheduling-gap-{location}"))

    def all_wide(payload):
        for row in payload["wakeup"]:
            if row["mechanism"] in ("mach_wait_until", "nanosleep"):
                row["timebase_control_tight_count"] = 0
                row["timebase_control_min_width_ns"] = row["target_ns"] // 200 + 1
    cases.append((changed(base, all_wide), False, "all-wide"))

    # Inclusive exact boundaries are valid.
    cases.append((changed(base, lambda p: arm(p).__setitem__(
        "timebase_control_max_lower_residual_ns", 0)), True,
        "exact-lower-boundary"))
    cases.append((changed(base, lambda p: arm(p).__setitem__(
        "timebase_control_max_upper_residual_ns", 0)), True,
        "exact-upper-boundary"))

    # Malformed, missing, inverted-domain, and overflow representations fail
    # closed instead of throwing or being skipped.
    cases.extend([
        (changed(base, lambda p: arm(p).pop("timebase_control_slack_ns")),
         False, "missing-field"),
        (changed(base, lambda p: arm(p).__setitem__(
            "timebase_control_slack_ns", "252")), False, "noninteger-field"),
        (changed(base, lambda p: arm(p).__setitem__(
            "timebase_control_n", 0)), False, "zero-control-count"),
        (changed(base, lambda p: arm(p).__setitem__(
            "timebase_control_invalid_count", 1)), False, "inverted-domain"),
        (changed(base, lambda p: arm(p).__setitem__(
            "timebase_control_max_lower_residual_ns", 1 << 63)),
         False, "residual-overflow"),
        (changed(base, lambda p: arm(p).__setitem__(
            "timebase_control_violation_count", arm(p)["n"] + 1)),
         False, "count-overflow"),
        (changed(base, lambda p: p["timebase"].__setitem__("denom", 0)),
         False, "zero-timebase-denominator"),
        (changed(base, lambda p: p["timebase"].__setitem__("numer", "125")),
         False, "noninteger-timebase"),
        (changed(base, lambda p: p["timebase"].__setitem__(
            "numer", (1 << 64) - 1)), False, "slack-overflow"),
    ])

    def mutate_resolution(payload, value):
        row = next(c for c in payload["clocks"]
                   if c["clock"] == "clock_gettime_nsec_np(CLOCK_UPTIME_RAW)")
        row["getres_ns"] = value
    cases.append((changed(base, lambda p: mutate_resolution(p, 0)),
                  False, "zero-clock-resolution"))
    cases.append((changed(base, lambda p: mutate_resolution(p, "42")),
                  False, "noninteger-clock-resolution"))

    def coarse_resolution(payload):
        mutate_resolution(payload, 1000)
        for row in payload["wakeup"]:
            row["timebase_control_slack_ns"] = 6000
    cases.append((changed(base, coarse_resolution), False, "slack-cap"))

    ok = True
    for payload, expect_pass, label in cases:
        ok = run_case(checker, payload, expect_pass, label) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

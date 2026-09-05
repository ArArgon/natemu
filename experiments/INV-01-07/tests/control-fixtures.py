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
    if label == "signal-asymmetric":
        required = (
            "MEET  T3", "= 90001 ns (cross-thread timestamp diagnostic only)",
            "full sender round-trip p99 (100001 ns)",
            "not future or worst-case delivery",
            "no half-round-trip directional bound",
        )
        if not all(item in proc.stdout for item in required):
            print(proc.stdout, file=sys.stderr)
            return False
    if label == "signal-above-roundtrip":
        failed_rules = [line for line in proc.stdout.splitlines()
                        if line.startswith("  FAIL")]
        if (len(failed_rules) != 1 or "I17" not in failed_rules[0]
                or "decision thresholds suppressed" not in proc.stdout):
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


def producer_case(producer, name):
    proc = subprocess.run(
        [str(producer), "--control-case", name],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"producer control case {name} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def install_control(row, values):
    for key, value in values.items():
        row[key] = value


def main():
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} CHECKER VALID_FIXTURE PRODUCER",
              file=sys.stderr)
        return 2
    checker, fixture, producer = map(Path, sys.argv[1:])
    base = json.loads(fixture.read_text())
    cases = [(base, True, "baseline")]

    # Exercise production summarize/emission rather than copying its formula.
    # Odd values catch integer-halving/truncation regressions as well.
    for name, expected_forward, expect_pass in (
        ("asymmetric", 90001, True),
        ("above-roundtrip", 110001, False),
    ):
        proc = subprocess.run(
            [str(producer), "--signal-summary-case", name],
            text=True, capture_output=True, check=True,
        )
        derived = json.loads(proc.stdout)
        assert derived["n"] == 2000
        assert derived["p99_ns"] == expected_forward
        assert derived["roundtrip_p99_ns"] == 100001
        assert derived["oneway_upper_bound_p99_ns"] == 100001
        assert derived["p99_ns"] > derived["roundtrip_p99_ns"] / 2
        payload = copy.deepcopy(base)
        idx = next(i for i, row in enumerate(payload["signal"])
                   if row["mechanism"] == derived["mechanism"]
                   and row["qos"] == derived["qos"])
        payload["signal"][idx] = derived
        cases.append((payload, expect_pass, f"signal-{name}"))

    # A ±1% conversion defect must fail in both directions at every target.
    for target in (100000, 1000000, 10000000):
        for direction in ("plus", "minus"):
            derived = producer_case(
                producer, f"conversion-{direction}-{target}")
            def inject(payload, target=target, derived=derived):
                install_control(arm(payload, target=target), derived)
            cases.append((changed(base, inject), False,
                          f"conversion-{direction}-1pct-{target}"))

    # Every placement is a distinct raw timestamp tuple driven through the
    # same conversion and aggregation seam production uses. One tight sample
    # in each derived aggregate preserves I16b.
    for target in (100000, 1000000, 10000000):
        for location in ("m0-c0", "c0-base", "woke-c1", "c1-m1"):
            derived = producer_case(producer, f"gap-{location}-{target}")
            def widen(payload, target=target, derived=derived):
                install_control(arm(payload, target=target), derived)
            cases.append((changed(base, widen), True,
                          f"scheduling-gap-{location}-{target}"))

    derived_wide = producer_case(producer, "all-wide-100000")
    cases.append((changed(base, lambda p: install_control(
        arm(p), derived_wide)), False, "all-wide"))

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

    # No dispatch diagnostic, however malformed, can affect I16a/I16b.
    def erase_dispatch_control(payload):
        row = arm(payload, mechanism="dispatch_source_timer_leeway0")
        for key in list(row):
            if key.startswith("timebase_control_"):
                del row[key]
    cases.append((changed(base, erase_dispatch_control), True,
                  "dispatch-report-only"))

    ok = True
    for payload, expect_pass, label in cases:
        ok = run_case(checker, payload, expect_pass, label) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

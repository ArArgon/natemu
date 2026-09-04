#!/usr/bin/env python3
"""Checker for INV-01-07 result.json.

Integrity rules detect a defective measurement and exit non-zero (FAIL).
Decision thresholds are predeclared in INV-01-07 and are only reported here;
missing a threshold is a finding about the host, not a code defect, and is
adjudicated at the result gate.
"""
import json
import sys

DEADLINE_MECHS = ("mach_wait_until", "dispatch_source_timer_leeway0")


def main(path):
    d = json.load(open(path))
    fails, notes = [], []

    def rule(ok, name, detail):
        (notes if ok else fails).append(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")

    # --- integrity rules ---
    tb = d.get("timebase", {})
    rule(tb.get("numer", 0) > 0 and tb.get("denom", 0) > 0, "I1 timebase",
         f"numer={tb.get('numer')} denom={tb.get('denom')}")

    clocks = d.get("clocks", [])
    rule(len(clocks) >= 5, "I2 clock coverage", f"{len(clocks)} clocks probed")
    bad = [c["clock"] for c in clocks if c["backward_reads"] > 0]
    rule(not bad, "I3 single-thread monotonicity", f"backward reads on {bad or 'none'}")

    x = d.get("cross_thread_clock", {})
    rule(x.get("inversions", 1) == 0, "I4 cross-thread monotonicity",
         f"inversions={x.get('inversions')} worst_backward_ns={x.get('worst_backward_ns')}")

    early = [(w["mechanism"], w["target_ns"], w["qos"], w["early"])
             for w in d.get("wakeup", [])
             if w["early"] > 0 and w["mechanism"] in DEADLINE_MECHS]
    rule(not early, "I5 no wake before deadline",
         f"{len(early)} deadline arms returned early: {early[:4]}")

    ns_early = [(w["target_ns"], w["qos"], w["early"]) for w in d.get("wakeup", [])
                if w["mechanism"] == "nanosleep" and w["early"] > 0]
    rule(not ns_early, "I6 nanosleep not short",
         f"{len(ns_early)} nanosleep arms slept less than requested: {ns_early[:4]}")

    short = [(w["mechanism"], w["target_ns"], w["qos"], w["n"])
             for w in d.get("wakeup", []) if w["n"] < 2000]
    rule(not short, "I7 wakeup sample size", f"arms below 2000 samples: {short[:4]}")

    neg = [(s["mechanism"], s["qos"], s["min_ns"]) for s in d.get("signal", [])
           if s["min_ns"] < 0]
    rule(not neg, "I8 signal latency non-negative", f"{neg[:4]}")

    lost = [(s["mechanism"], s["qos"], s["lost"]) for s in d.get("signal", [])
            if s["lost"] > 0]
    rule(not lost, "I9 no lost cross-thread events", f"{lost[:4]}")

    ssz = [(s["mechanism"], s["qos"], s["n"]) for s in d.get("signal", [])
           if s["n"] < 2000]
    rule(not ssz, "I10 signal sample size", f"arms below 2000 samples: {ssz[:4]}")

    print(f"INV-01-07 checker  mode={d.get('mode')}  file={path}")
    for line in notes + fails:
        print("  " + line)

    # --- predeclared decision thresholds (reported, never a code FAIL) ---
    print("  -- decision thresholds (result gate adjudicates) --")

    def thr(name, ok, detail):
        print(f"  {'MEET' if ok else 'MISS'}  {name}: {detail}")

    fastest = min((c["read_cost_p50_ns"] for c in clocks), default=None)
    thr("T1 clock read cost p50 < 100 ns", fastest is not None and fastest < 100,
        f"fastest clock read p50 = {fastest} ns")

    ui1ms = [w for w in d.get("wakeup", [])
             if w["target_ns"] == 1000000 and w["qos"] == "user-interactive"]
    best = min((w["p50_ns"] for w in ui1ms), default=None)
    thr("T2 1 kHz tick: median wakeup error < 250 us at user-interactive",
        best is not None and best < 250000,
        f"best median error at 1 ms target = {best} ns "
        f"({[(w['mechanism'], w['p50_ns']) for w in ui1ms]})")

    ipi = [s for s in d.get("signal", []) if s["qos"] == "user-interactive"]
    bp99 = min((s["p99_ns"] for s in ipi), default=None)
    thr("T3 IPI analogue: p99 delivery < 100 us at user-interactive",
        bp99 is not None and bp99 < 100000,
        f"best p99 = {bp99} ns ({[(s['mechanism'], s['p99_ns']) for s in ipi]})")

    bg = [w for w in d.get("wakeup", []) if w["qos"] == "background"]
    ui = [w for w in d.get("wakeup", []) if w["qos"] == "user-interactive"]
    if bg and ui:
        rb = max(w["p50_ns"] for w in bg) / max(1, max(w["p50_ns"] for w in ui))
        thr("T4 QoS sensitivity < 5x (placement/coalescing exposure)", rb < 5,
            f"worst background/user-interactive median ratio = {rb:.1f}x")

    print(f"RESULT: {'FAIL' if fails else 'PASS'} ({len(fails)} integrity failures)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "result.json"))

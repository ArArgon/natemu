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
CLOCKS = {
    "mach_absolute_time",
    "clock_gettime_nsec_np(CLOCK_UPTIME_RAW)",
    "clock_gettime(CLOCK_MONOTONIC_RAW)",
    "clock_gettime(CLOCK_MONOTONIC)",
    "clock_gettime(CLOCK_UPTIME_RAW)",
    "clock_gettime(CLOCK_REALTIME)",
}
QOS = {"user-interactive", "background"}
UINT64_MAX = (1 << 64) - 1
INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1
MONOTONIC_CLOCKS = {
    "mach_absolute_time",
    "clock_gettime_nsec_np(CLOCK_UPTIME_RAW)",
    "clock_gettime(CLOCK_MONOTONIC_RAW)",
    "clock_gettime(CLOCK_MONOTONIC)",
    "clock_gettime(CLOCK_UPTIME_RAW)",
}
WAKEUP_CASES = {
    (mechanism, target, qos)
    for mechanism in ("mach_wait_until", "nanosleep",
                      "dispatch_source_timer_leeway0",
                      "dispatch_source_timer_strict")
    for target in (100000, 1000000, 10000000)
    for qos in QOS
}
CONCURRENT_THREADS = {2, 4, 8}
SIGNAL_CASES = {
    (mechanism, qos)
    for mechanism in ("pthread_cond_signal", "dispatch_semaphore_signal",
                      "pthread_kill_SIGUSR1_spinning")
    for qos in QOS
}


def is_u64(value, *, positive=False):
    return (isinstance(value, int) and not isinstance(value, bool)
            and (value > 0 if positive else value >= 0)
            and value <= UINT64_MAX)


def is_i64(value):
    return (isinstance(value, int) and not isinstance(value, bool)
            and INT64_MIN <= value <= INT64_MAX)


def main(path):
    d = json.load(open(path))
    fails, notes = [], []

    def rule(ok, name, detail):
        (notes if ok else fails).append(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")

    # --- integrity rules ---
    rule(d.get("experiment") == "INV-01-07", "I0 experiment identity",
         f"experiment={d.get('experiment')}")
    tb = d.get("timebase", {})
    tb_numer, tb_denom = tb.get("numer"), tb.get("denom")
    tb_ok = is_u64(tb_numer, positive=True) and is_u64(tb_denom, positive=True)
    rule(tb_ok, "I1 timebase", f"numer={tb_numer} denom={tb_denom}")

    clocks = d.get("clocks", [])
    clock_names = [c.get("clock") for c in clocks]
    rule(len(clock_names) == len(CLOCKS) and set(clock_names) == CLOCKS,
         "I2 exact clock coverage",
         f"expected {len(CLOCKS)} unique clocks; got {len(clock_names)} rows / "
         f"{len(set(clock_names))} unique")
    bad = [c["clock"] for c in clocks if c["backward_reads"] > 0]
    rule(not bad, "I3 single-thread monotonicity", f"backward reads on {bad or 'none'}")

    x = d.get("cross_thread_clock", {})
    rule(x.get("inversions", 1) == 0, "I4 cross-thread monotonicity",
         f"inversions={x.get('inversions')} worst_backward_ns={x.get('worst_backward_ns')}")

    wakeup = d.get("wakeup", [])
    wakeup_cases = [(w.get("mechanism"), w.get("target_ns"), w.get("qos"))
                    for w in wakeup]
    rule(len(wakeup_cases) == len(WAKEUP_CASES) and set(wakeup_cases) == WAKEUP_CASES,
         "I5 exact wakeup-arm coverage",
         f"expected {len(WAKEUP_CASES)} unique arms; got {len(wakeup_cases)} rows / "
         f"{len(set(wakeup_cases))} unique")
    early = [(w["mechanism"], w["target_ns"], w["qos"], w["early"])
             for w in wakeup
             if w["early"] > 0 and w["mechanism"] in DEADLINE_MECHS]
    rule(not early, "I6 no wake before deadline",
         f"{len(early)} deadline arms returned early: {early[:4]}")

    ns_early = [(w["target_ns"], w["qos"], w["early"]) for w in wakeup
                if w["mechanism"] == "nanosleep" and w["early"] > 0]
    rule(not ns_early, "I7 nanosleep not short",
         f"{len(ns_early)} nanosleep arms slept less than requested: {ns_early[:4]}")

    short = [(w["mechanism"], w["target_ns"], w["qos"], w["n"])
             for w in wakeup if w["n"] < 2000]
    rule(not short, "I8 wakeup sample size", f"arms below 2000 samples: {short[:4]}")

    signal = d.get("signal", [])
    signal_cases = [(s.get("mechanism"), s.get("qos")) for s in signal]
    rule(len(signal_cases) == len(SIGNAL_CASES) and set(signal_cases) == SIGNAL_CASES,
         "I9 exact signal-arm coverage",
         f"expected {len(SIGNAL_CASES)} unique arms; got {len(signal_cases)} rows / "
         f"{len(set(signal_cases))} unique")
    neg = [(s["mechanism"], s["qos"], s["min_ns"]) for s in signal
           if s["min_ns"] < 0]
    rule(not neg, "I10 signal latency non-negative", f"{neg[:4]}")

    lost = [(s["mechanism"], s["qos"], s["lost"]) for s in signal
            if s["lost"] > 0]
    rule(not lost, "I11 no lost cross-thread events", f"{lost[:4]}")

    ssz = [(s["mechanism"], s["qos"], s["n"]) for s in signal
           if s["n"] < 2000]
    rule(not ssz, "I12 signal sample size", f"arms below 2000 samples: {ssz[:4]}")

    cw = d.get("concurrent_wakeup", [])
    cw_threads = [c.get("threads") for c in cw]
    rule(len(cw_threads) == len(CONCURRENT_THREADS)
         and set(cw_threads) == CONCURRENT_THREADS,
         "I13 exact concurrent-wakeup coverage",
         f"expected {sorted(CONCURRENT_THREADS)} thread counts; got {cw_threads}")
    cw_early = [(c["threads"], c["early"]) for c in cw if c["early"] > 0]
    rule(not cw_early, "I14 no concurrent wake before deadline", f"{cw_early}")
    cw_small = [(c["threads"], c["n"]) for c in cw
                if c["n"] < 2000 * c.get("threads", 1)]
    rule(not cw_small, "I15 concurrent-wakeup sample size",
         f"arms below 2000 samples per thread: {cw_small}")

    # Accepted containment control (REV-139). For ordered reads
    # m0,c0,base ... woke,c1,m1, the independently measured c1-c0 interval must
    # be contained between the converted-Mach inner and outer intervals. The
    # producer emits aggregate diagnostics; the checker derives slack from the
    # recorded timebase and CLOCK_UPTIME_RAW resolution and checks both QoS
    # classes for the same-thread mechanisms. Dispatch remains report-only
    # because its wake timestamp is taken on another thread.
    SAME_THREAD = ("mach_wait_until", "nanosleep")
    raw_rows = [c for c in clocks
                if c.get("clock") == "clock_gettime_nsec_np(CLOCK_UPTIME_RAW)"]
    raw_resolution = raw_rows[0].get("getres_ns") if len(raw_rows) == 1 else None
    control_slack = None
    if tb_ok and is_u64(raw_resolution, positive=True):
        mach_quantum = (tb_numer + tb_denom - 1) // tb_denom
        quantum = max(mach_quantum, raw_resolution)
        if quantum <= UINT64_MAX // 6:
            control_slack = quantum * 6

    control_fields = (
        "timebase_control_n",
        "timebase_control_slack_ns",
        "timebase_control_invalid_count",
        "timebase_control_violation_count",
        "timebase_control_max_lower_residual_ns",
        "timebase_control_max_upper_residual_ns",
        "timebase_control_min_width_ns",
        "timebase_control_tight_count",
    )
    malformed, containment_bad, nonvacuous_bad = [], [], []
    for w in wakeup:
        ident = (w.get("mechanism"), w.get("target_ns"), w.get("qos"))
        # Dispatch wake timestamps are captured on another thread. Its control
        # object is diagnostic only: no value or shape in it may affect I16.
        if w.get("mechanism") not in SAME_THREAD:
            continue
        values = {name: w.get(name) for name in control_fields}
        n = values["timebase_control_n"]
        slack = values["timebase_control_slack_ns"]
        invalid = values["timebase_control_invalid_count"]
        violations = values["timebase_control_violation_count"]
        lower = values["timebase_control_max_lower_residual_ns"]
        upper = values["timebase_control_max_upper_residual_ns"]
        min_width = values["timebase_control_min_width_ns"]
        tight = values["timebase_control_tight_count"]
        shape_ok = (
            is_u64(n, positive=True)
            and is_u64(w.get("n"), positive=True) and n == w.get("n")
            and is_u64(slack, positive=True) and slack == control_slack
            and is_u64(invalid) and invalid <= n
            and is_u64(violations) and violations <= n
            and is_i64(lower) and is_i64(upper)
            and is_u64(min_width)
            and is_u64(tight) and tight <= n
        )
        if not shape_ok:
            malformed.append((ident, values, control_slack))
            continue
        target = w.get("target_ns")
        if not is_u64(target, positive=True):
            malformed.append((ident, values, control_slack))
            continue
        tight_limit = target // 200
        if (slack > tight_limit or invalid != 0 or violations != 0
                or lower > 0 or upper > 0):
            containment_bad.append((ident, invalid, violations, lower, upper,
                                    slack, tight_limit))
        if tight < 1 or min_width > tight_limit:
            nonvacuous_bad.append((ident, tight, min_width, tight_limit))

    rule(not malformed and not containment_bad,
         "I16a per-sample timebase containment",
         "all wakeup diagnostics well-formed and every same-thread sample at "
         "both QoS classes contained; "
         f"malformed={malformed[:2] or 'none'} "
         f"violations={containment_bad[:4] or 'none'}")
    rule(not malformed and not nonvacuous_bad,
         "I16b per-arm timebase non-vacuity",
         "every same-thread arm has at least one bracket no wider than 0.5% "
         f"of target; malformed={malformed[:2] or 'none'} "
         f"wide={nonvacuous_bad[:4] or 'none'}")

    # The round-trip control (REV-044 change 1): one-way latency subtracts two
    # threads' timestamps, so it must never exceed the sender-only round trip.
    rt_bad = [(x["mechanism"], x["qos"], x["p99_ns"], x.get("roundtrip_p99_ns"))
              for x in signal
              if x.get("roundtrip_p99_ns") is None
              or x["p99_ns"] > x["roundtrip_p99_ns"]]
    rule(not rt_bad, "I17 one-way latency within the sender-only round trip",
         f"{rt_bad[:4]}")

    print(f"INV-01-07 checker  mode={d.get('mode')}  file={path}")
    for line in notes + fails:
        print("  " + line)

    if fails:
        # A threshold computed from a run that failed an integrity rule is not
        # interpretable, and a MEET printed next to a physically impossible
        # value invites exactly the misreading REV-045 found. Suppress.
        print("  -- decision thresholds suppressed: integrity failures present --")
        print(f"RESULT: FAIL ({len(fails)} integrity failures)")
        return 1

    # --- predeclared decision thresholds (reported, never a code FAIL) ---
    print("  -- decision thresholds (result gate adjudicates) --")

    def thr(name, ok, detail):
        print(f"  {'MEET' if ok else 'MISS'}  {name}: {detail}")

    # CLM-01-07-11 is about "the fastest documented monotonic clock", so the
    # settable wall clock must not be able to satisfy it (REV-045 change 4).
    mono = [c for c in clocks if c["clock"] in MONOTONIC_CLOCKS]
    fastest = min((c["read_cost_p50_ns"] for c in mono), default=None)
    thr("T1 fastest monotonic clock read p50 < 100 ns "
        "(guest RDTSC budget: 1 kHz tick x 8 vCPUs plus in-guest reads must "
        "stay under 1% of a core, so a read must cost far less than 1 us)",
        fastest is not None and fastest < 100,
        f"fastest monotonic clock read p50 = {fastest} ns over "
        f"{len(mono)} monotonic clocks; CLOCK_REALTIME excluded")

    ui1ms = [w for w in d.get("wakeup", [])
             if w["target_ns"] == 1000000 and w["qos"] == "user-interactive"]
    best = min((w["p50_ns"] for w in ui1ms), default=None)
    thr("T2 1 kHz tick: median wakeup error < 250 us at user-interactive "
        "(derived: one quarter of the 1 ms period of the declared 1 kHz "
        "guest tick, so four ticks' error cannot accumulate into a lost tick)",
        best is not None and best < 250000,
        f"best median error at 1 ms target = {best} ns "
        f"({[(w['mechanism'], w['p50_ns']) for w in ui1ms]}). "
        "A miss routes per INV-01-07 Exclusion and stop criteria: first to "
        "Q-01-22 (guest time-base policy) and Q-01-21 (power/background "
        "state), then to a narrowed applicability stratum; it becomes a KA-05 "
        "failure only if no admissible mechanism, including "
        "DISPATCH_TIMER_STRICT, meets it at any accepted stratum.")

    ipi = [s for s in signal
           if s["qos"] == "user-interactive"
           and s["mechanism"] == "pthread_kill_SIGUSR1_spinning"]
    # T3 is the ROUND-TRIP quantity, matching the record's Metrics table and
    # CLM-01-07-15. The one-way figure is not a standalone threshold: it
    # subtracts two threads' timestamps and no source in the inventory states
    # that the clock is a system-wide comparable time base. Rule I17 only
    # requires one-way <= round trip, so evaluating T3 on the one-way p99
    # would let a run with a 90 us one-way and a 250 us round trip print MEET
    # while missing the record's bar (REV-064/REV-065 change 1).
    rt99 = min((s["roundtrip_p99_ns"] for s in ipi
                if s.get("roundtrip_p99_ns") is not None), default=None)
    ow99 = min((s["p99_ns"] for s in ipi), default=None)
    thr("T3 IPI analogue: round-trip p99 < 200 us at user-interactive "
        "(derived: one fifth of the 1 ms guest tick period, so a cross-vCPU "
        "interrupt and its acknowledgment complete inside the tick that "
        "raised them)",
        rt99 is not None and rt99 < 200000,
        f"pthread_kill spinning-target round-trip p99 = {rt99} ns; one-way p99 "
        f"= {ow99} ns (cross-thread timestamp diagnostic only); full sender "
        f"round-trip p99 ({rt99} ns) bounds the causal forward component's "
        f"empirical p99 for the same completed samples, not future or "
        f"worst-case delivery; no half-round-trip directional bound")

    by_arm = {(w["mechanism"], w["target_ns"], w["qos"]): w for w in wakeup}
    ratios = [
        by_arm[(m, t, "background")]["p50_ns"]
        / max(1, by_arm[(m, t, "user-interactive")]["p50_ns"])
        for m in ("mach_wait_until", "nanosleep",
                  "dispatch_source_timer_leeway0", "dispatch_source_timer_strict")
        for t in (100000, 1000000, 10000000)
        if (m, t, "background") in by_arm and (m, t, "user-interactive") in by_arm
    ]
    if ratios:
        rb = max(ratios)
        thr("T4 QoS-class effect on wakeup error < 5x", rb < 5,
            f"worst matched-arm background/user-interactive median ratio = "
            f"{rb:.1f}x. This is a QoS-class effect only; no part of it may be "
            "attributed to core type (CLM-01-07-08 — placement is sampled, "
            "never controlled or observed).")

    print(f"RESULT: {'FAIL' if fails else 'PASS'} ({len(fails)} integrity failures)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "result.json"))

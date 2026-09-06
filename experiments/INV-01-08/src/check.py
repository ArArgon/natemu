#!/usr/bin/env python3
"""Checker for INV-01-08 result.json.

Integrity rules detect a defective measurement and exit non-zero (FAIL). The
most important one is the positive control: if the plain (undecorated) SB
litmus never observes the Arm-permitted relaxed outcome, the harness cannot
expose weak behaviour at all and every "not observed" result below it is
uninterpretable.

Non-observation is never proof of prohibition; the architectural claims come
from the cited Arm ARM and Intel SDM sections, not from this checker.
"""
import argparse
import contextlib
import io
import json
import sys
import math
from fractions import Fraction

from histogram import load_cohort, report, validate_histograms
from fallback import validate_fallback, fallback_ratio, validate_full_counts
from evidence import json_read, validate_result_binding

MIN_ITERS = 1000000
QOS = {"user-interactive", "background"}
LITMUS_CASES = {
    (test, variant, qos)
    for test in ("SB", "MP")
    for variant in ("plain-ldr-str", "ldar-stlr", "dmb-ish")
    for qos in QOS
}
LSE_CASES = {(op, threads) for op in ("ldadd", "cas") for threads in (2, 4, 8)}
# Arms whose relaxed outcome the cited architecture forbids, so a nonzero count
# means the harness is broken (or the observation contradicts the source) and
# must be investigated before anything rests on the run.
#
# SB/ldar-stlr is forbidden per SRC-108 (Pulte et al., POPL 2018), whose
# axiomatic model's full bob union includes po; [dmb.full]; po and
# [L]; po; [A] (these are two relevant clauses, not the full definition), and states
# that "the [L];po;[A] case of the bob relation describes a store-release
# appearing in program order before a load-acquire, which is required to be
# observed in order by all threads". An earlier revision of this checker
# omitted this arm on the unsourced assertion that STLR followed by LDAR gives
# no Store->Load ordering; REV-043 and REV-045 returned that, and it is
# restored here.
FORBIDDEN_ARMS = {
    ("SB", "ldar-stlr"),
    ("SB", "dmb-ish"),
    ("MP", "ldar-stlr"),
    ("MP", "dmb-ish"),
}
ALIGNMENT_CASES = {
    "ldaddal aligned 4B (control)",
    "ldaddal misaligned within 16B granule",
    "ldaddal crossing 16-byte boundary",
    "ldaddal crossing 64-byte cache line",
    "plain ldr crossing 64-byte cache line",
    "ldar crossing 64-byte cache line",
}


def main(path, data=None, integrity_only=False, historical=False):
    if data is None:
        data = json_read(path)
    d = data
    fails, notes = [], []

    if historical and not integrity_only and "schema" in d:
        raise ValueError("historical mode cannot bypass F08-2 integrity")

    if not historical:
        failures = validate_fallback(d) + validate_full_counts(d)
        if failures:
            print("\n".join(failures))
            print("RESULT: FAIL (F08-2 integrity; all histogram/threshold claims suppressed)")
            return 1
        timing = validate_result_binding(path, d)

    def rule(ok, name, detail):
        (notes if ok else fails).append(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")

    lit = d.get("litmus", [])
    litmus_cases = [(l.get("test"), l.get("variant"), l.get("qos")) for l in lit]
    rule(len(litmus_cases) == len(LITMUS_CASES) and set(litmus_cases) == LITMUS_CASES,
         "I1 exact litmus-arm coverage",
         f"expected {len(LITMUS_CASES)} unique arms; got {len(litmus_cases)} rows / "
         f"{len(set(litmus_cases))} unique")
    plain_sb = [l for l in lit if l["test"] == "SB" and l["variant"] == "plain-ldr-str"]
    rule(any(l["relaxed_observed"] > 0 for l in plain_sb),
         "I2 positive control (plain SB observes the relaxed outcome)",
         f"{[(l['qos'], l['relaxed_observed']) for l in plain_sb]}")

    seq = [l for l in lit if l.get("sequential")]
    rule(not seq, "I3 litmus threads ran concurrently",
         f"{len(seq)} arms ran sequentially")

    small = [(l["test"], l["variant"], l["qos"], l["iterations"])
             for l in lit if l["iterations"] < MIN_ITERS]
    rule(not small, "I4 litmus iteration count",
         f"arms below {MIN_ITERS}: {small[:4]}")

    forbidden = [
        (l["test"], l["variant"], l["qos"], l["relaxed_observed"])
        for l in lit
        if l["relaxed_observed"] > 0 and (l["test"], l["variant"]) in FORBIDDEN_ARMS
    ]
    rule(not forbidden,
         "I5 no architecture-forbidden relaxed outcome",
         f"forbidden arms {sorted(FORBIDDEN_ARMS)} with a nonzero count: "
         f"{forbidden[:4]}")

    lse = d.get("lse", [])
    lse_cases = [(a.get("op"), a.get("threads")) for a in lse]
    rule(len(lse_cases) == len(LSE_CASES) and set(lse_cases) == LSE_CASES,
         "I6 exact LSE-arm coverage",
         f"expected {len(LSE_CASES)} unique arms; got {len(lse_cases)} rows / "
         f"{len(set(lse_cases))} unique")
    lost = [(a["op"], a["threads"], a["lost_updates"]) for a in lse
            if a["lost_updates"] != 0]
    rule(not lost, "I7 LSE atomics lose no updates", f"{lost[:4]}")

    al = d.get("alignment", [])
    alignment_cases = [a.get("case") for a in al]
    rule(len(alignment_cases) == len(ALIGNMENT_CASES)
         and set(alignment_cases) == ALIGNMENT_CASES,
         "I8 exact alignment-case coverage",
         f"expected {len(ALIGNMENT_CASES)} unique cases; got {len(alignment_cases)} rows / "
         f"{len(set(alignment_cases))} unique")
    ctrl = [a for a in al if "aligned 4B (control)" in a["case"]]
    rule(ctrl and not ctrl[0]["faulted"], "I9 aligned-atomic control does not fault",
         f"{ctrl[0] if ctrl else 'control missing'}")
    plain = [a for a in al if a.get("case") == "plain ldr crossing 64-byte cache line"]
    rule(plain and not plain[0]["faulted"],
         "I10 ordinary-load contrast does not fault",
         f"{plain[0] if plain else 'control missing'}")
    faulted_no_sig = [a for a in al if a["faulted"] and a["signal"] == 0]
    unmapped = [a for a in al if a["faulted"]
                and (a["signal"], a["si_code"]) != (10, 1)]
    rule(True, "I11a fault symbol mapping (informational)",
         "signal 10 = SIGBUS, si_code 1 = BUS_ADRALN on this platform; "
         f"cases with a different (signal, si_code): {unmapped[:4] or 'none'}")
    rule(not faulted_no_sig, "I11 every fault carries a signal number",
         f"{faulted_no_sig[:2]}")

    rt = d.get("roundtrip", [])
    rt_qos = [r.get("qos") for r in rt]
    rule(len(rt_qos) == len(QOS) and set(rt_qos) == QOS,
         "I12 exact round-trip coverage",
         f"expected {sorted(QOS)}; got {rt_qos}")
    to = [(r["qos"], r["timeouts"]) for r in rt if r["timeouts"] > 0]
    rule(not to, "I13 invalidation round trip always acknowledged", f"{to}")
    rule(all(r["min_ns"] > 0 for r in rt) and rt,
         "I14 round-trip latency positive", f"{[(r['qos'], r['min_ns']) for r in rt]}")

    print(f"INV-01-08 checker  mode={d.get('mode')}  file={path}")
    for line in notes + fails:
        print("  " + line)

    if fails:
        # A threshold computed from a run that failed an integrity rule is not
        # interpretable, and a MEET printed next to a physically impossible
        # value invites exactly the misreading REV-045 found. Suppress.
        #
        # REV-210 R1: an intended failure (e.g. the sequential-negative
        # control's exact I2/I3) must not be a blank check for the rest of
        # the input. Require the unaffected histogram cells to still be a
        # valid, self-consistent accounting before this return is trusted as
        # "only the intended rule failed"; a corrupted/missing/mismatched
        # histogram is reported as its own failure, never silently ignored.
        try:
            validate_histograms(d)
        except (KeyError, TypeError, ValueError) as exc:
            print(f"  FAIL  H-input reporting validation: {exc}")
        print("  -- decision thresholds suppressed: integrity failures present --")
        print(f"RESULT: FAIL ({len(fails)} integrity failures)")
        return 1

    try:
        validate_histograms(d)
    except (KeyError, TypeError, ValueError) as exc:
        print(f"  FAIL  H-input reporting validation: {exc}")
        print("  -- histogram and decision thresholds suppressed: invalid reporting input --")
        print("RESULT: FAIL (reporting input)")
        return 1
    if integrity_only:
        return 0

    if not historical and timing.get("status")!="context-matched":
        print("  TIMING UNQUALIFIED: applicable clock evidence absent; correctness checks passed")
        return 0
    print("  -- decision thresholds (findings review adjudicates) --")

    def thr(name, ok, detail):
        print(f"  {'MEET' if ok else 'MISS'}  {name}: {detail}")

    # REV-167: exact three-way diagnostic and separate exposure eligibility.
    report([d])

    cross16 = [a for a in al if "crossing 16-byte boundary" in a["case"]]
    if cross16:
        a = cross16[0]
        thr("T1 cross-16-byte atomic faults on this host",
            a["faulted"],
            f"faulted={a['faulted']} signal={a['signal']} si_code={a['si_code']} "
            "(categorical host outcome; a completion is not evidence of non-atomicity)")

    fb = d.get("fallback_global_lock", {})
    if historical:
        # Check any historical display's syntax without treating it as an
        # observed correctness oracle or qualified timing measurement.
        if "ns_per_op" in fb and (type(fb["ns_per_op"]) not in (int, float) or
                                  not math.isfinite(fb["ns_per_op"])):
            raise TypeError("malformed historical display")
        print("  T2/T3 UNQUALIFIED: historical fallback oracle and prior clock prerequisite absent")
    else:
        ratio = fallback_ratio(d)
        thr("T2 software fallback aggregate harness cost under 500 ns/op at 4 threads",
            ratio < 500, f"{ratio.numerator}/{ratio.denominator} ns/completed-op (exact)")

    ui = [r for r in rt if r["qos"] == "user-interactive"]
    tb = d.get("timebase", {})
    tick = (tb.get("numer", 0) / tb.get("denom", 1)) if tb.get("denom") else 0
    if not historical:
        thr("T3 cross-thread invalidation round trip p99 < 50 us",
            bool(ui) and ui[0]["p99_ns"] < 50000,
            f"{[(r['qos'], r['p50_ns'], r['p99_ns']) for r in rt]}; "
            f"clock quantum {tick:.2f} ns/tick; prior instrument context matched; "
            "differences at its floor are unresolved")

    lse8 = [a for a in d.get("lse", []) if a["threads"] == 8]
    lse2 = [a for a in d.get("lse", []) if a["threads"] == 2]
    if lse8 and lse2:
        # Both denominators describe aggregate harness cost, including
        # creation/join, scheduling, loops and CAS loads/retries. Neither
        # isolates instruction latency or a guest workload cost floor.
        for op, key, label, bound in (
                ("ldadd", "ns_per_attempt", "T4a ldadd", 20),
                ("cas", "ns_per_attempt", "T4a cas harness per-attempt", 20),
                ("cas", "ns_per_op", "T4b cas loop (retries included)", 60)):
            a8 = [x for x in lse8 if x["op"] == op]
            a2 = [x for x in lse2 if x["op"] == op]
            if a8 and a2 and key in a8[0] and key in a2[0]:
                denominator = "attempts" if key == "ns_per_attempt" else "ops"
                for a in (a2[0], a8[0]):
                    if any(type(a.get(k)) is not int or a[k] <= 0 for k in ("elapsed_ns", denominator)):
                        raise ValueError("invalid aggregate primitive denominator")
                v2 = Fraction(a2[0]["elapsed_ns"], a2[0][denominator])
                v8 = Fraction(a8[0]["elapsed_ns"], a8[0][denominator])
                ratio = v8 / v2
                thr(f"{label} 8-thread/2-thread cost ratio < {bound}x",
                    ratio < bound,
                    f"{float(v2):.2f} -> {float(v8):.2f} ns ({key}); exact ratio="
                    f"{ratio.numerator}/{ratio.denominator} ({float(ratio):.13g}x); "
                    f"attempts/success 2T={a2[0].get('attempts_per_success', 1):.2f} "
                    f"8T={a8[0].get('attempts_per_success', 1):.2f}")

    print("HISTORICAL: bounded I/H and aggregate ratios checked; not F08-2 PASS" if historical else
          "RESULT: PASS (F08-2 I/H/F integrity; result/replication gates remain separate)")
    return 1 if fails else 0


def run_selected(args):
    if not args.cohort:
        return main(args.path or "result.json", historical=args.historical)
    identity, digest, cohort_path, runs = load_cohort(args.cohort)
    if args.historical and any("schema" in data for _, data in runs):
        raise ValueError("historical cohort cannot bypass F08-2 integrity")
    for path, data in runs:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            rc = main(path, data=data, integrity_only=True, historical=args.historical)
        if rc:
            print(captured.getvalue(), end="")
            print("RESULT: FAIL (entire cohort suppressed; failed member)")
            return 1
    print(f"INV-01-08 pooled histogram cohort={identity} sha256={digest} source={cohort_path}")
    for path, _ in runs:
        print(f"  member={path}")
    report([data for _, data in runs])
    print("RESULT: PASS (explicit cohort histogram only; no pooled timing thresholds; "
          + ("historical, not F08-2" if args.historical else "F08-2 member integrity") + ")")
    return 0


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", help="one result.json; no automatic pooling")
    parser.add_argument("--cohort", help="explicit digest-bound independently reviewed cohort JSON")
    parser.add_argument("--historical", action="store_true", help="explicit legacy I/H interpretation; no F08-2/T2/T3 qualification")
    args = parser.parse_args(argv)
    if args.path and args.cohort:
        parser.error("choose one result or --cohort")
    # REV-182: late input/arithmetic/rendering errors must not leak earlier
    # eligible histogram rows or timing claims. Buffer the entire selected
    # report, including the pooled path; publish only after normal completion.
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            rc = run_selected(args)
    except Exception as exc:
        # Also cover unexpected rendering failures, not just schema errors.
        # Exception details may contain input/partially rendered report text;
        # emit only the exception type so they cannot reintroduce claim rows.
        print("RESULT: FAIL (input/report rejected; no histogram/threshold claims): "
              f"{type(exc).__name__}")
        return 1
    print(output.getvalue(), end="")
    return rc


if __name__ == "__main__":
    sys.exit(cli())

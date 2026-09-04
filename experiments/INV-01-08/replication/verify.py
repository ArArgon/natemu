#!/usr/bin/env python3
"""Independent checker for an INV-01-08 result.json.

Written by reviewer context `t01_events_reviewer` for REV-086. It does not
import, call, or copy the author's src/check.py: it recomputes the invariants
and the derived fields from the raw primitives (counts, elapsed_ns, attempts)
so that a defect in the probe's own arithmetic would show up as a
disagreement rather than being carried through.

Exit 0 if every invariant holds, 1 otherwise. Thresholds are reported, never
a failure: a threshold miss is a finding about the host.
"""
import json
import sys

MIN_ITERS = 1000000
QOS = ("background", "user-interactive")
TESTS = ("SB", "MP")
VARIANTS = ("plain-ldr-str", "ldar-stlr", "dmb-ish")
FORBIDDEN = {(t, v) for t in TESTS for v in ("ldar-stlr", "dmb-ish")}
ALIGN = ("ldaddal aligned 4B (control)",
         "ldaddal misaligned within 16B granule",
         "ldaddal crossing 16-byte boundary",
         "ldaddal crossing 64-byte cache line",
         "plain ldr crossing 64-byte cache line",
         "ldar crossing 64-byte cache line")
# For SB the relaxed (TSO-forbidden) outcome is r0==0 and r1==0; for MP it is
# flag seen without data, i.e. r0!=0 and r1==0.
RELAXED_CELL = {"SB": "r0=0,r1=0", "MP": "r0!=0,r1=0"}
BOTH_CELL = "r0!=0,r1!=0"


def main(path):
    d = json.load(open(path))
    fails, oks = [], []

    def r(ok, name, detail):
        (oks if ok else fails).append(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")

    r(d.get("experiment") == "INV-01-08", "R0 experiment identity", d.get("experiment"))
    r(d.get("mode") == "normal", "R1 mode is a normal run", d.get("mode"))
    tb = d.get("timebase", {})
    r(tb.get("numer", 0) > 0 and tb.get("denom", 0) > 0, "R2 timebase",
      f"{tb.get('numer')}/{tb.get('denom')}")

    lit = d.get("litmus", [])
    seen = {(l["test"], l["variant"], l["qos"]) for l in lit}
    want = {(t, v, q) for t in TESTS for v in VARIANTS for q in QOS}
    r(len(lit) == len(want) and seen == want, "R3 litmus arm coverage",
      f"{len(lit)} rows, {len(seen)} unique")

    # The relaxed count must equal its own histogram cell; a disagreement means
    # the scoring and the histogram disagree about the same iterations.
    bad = [(l["test"], l["variant"], l["qos"], l["relaxed_observed"],
            (l.get("histogram") or {}).get(RELAXED_CELL[l["test"]]))
           for l in lit
           if (l.get("histogram") or {}).get(RELAXED_CELL[l["test"]]) != l["relaxed_observed"]]
    r(not bad, "R4 relaxed count equals its histogram cell", bad[:4] or "all 12 agree")

    tot = [(l["test"], l["variant"], l["qos"], sum((l.get("histogram") or {}).values()),
            l["iterations"])
           for l in lit if sum((l.get("histogram") or {}).values()) != l["iterations"]]
    r(not tot, "R5 histogram totals equal the iteration count", tot[:4] or "all 12 agree")

    r(not [l for l in lit if l["iterations"] < MIN_ITERS], "R6 full iteration count",
      f"min={min((l['iterations'] for l in lit), default=0)}")
    r(not [l for l in lit if l.get("sequential")], "R7 threads ran concurrently",
      f"{len([l for l in lit if l.get('sequential')])} sequential arms")

    plain_sb = [l for l in lit if (l["test"], l["variant"]) == ("SB", "plain-ldr-str")]
    r(any(l["relaxed_observed"] > 0 for l in plain_sb), "R8 positive control fires",
      {l["qos"]: l["relaxed_observed"] for l in plain_sb})
    forb = [(l["test"], l["variant"], l["qos"], l["relaxed_observed"]) for l in lit
            if (l["test"], l["variant"]) in FORBIDDEN and l["relaxed_observed"] > 0]
    r(not forb, "R9 no architecture-forbidden outcome", forb[:4] or "none")

    lse = d.get("lse", [])
    r({(a["op"], a["threads"]) for a in lse} ==
      {(o, n) for o in ("ldadd", "cas") for n in (2, 4, 8)} and len(lse) == 6,
      "R10 LSE arm coverage", f"{len(lse)} rows")
    r(not [a for a in lse if a["lost_updates"] != 0], "R11 no lost updates",
      [(a["op"], a["threads"], a["lost_updates"]) for a in lse if a["lost_updates"]] or "all zero")
    r(not [a for a in lse if a["attempts"] < a["ops"]], "R12 attempts >= successes",
      [(a["op"], a["threads"]) for a in lse if a["attempts"] < a["ops"]] or "ok")
    # Recompute the derived rates from elapsed_ns rather than trusting them.
    drift = []
    for a in lse:
        for key, div in (("ns_per_op", a["ops"]), ("ns_per_attempt", a["attempts"])):
            mine = a["elapsed_ns"] / div
            if abs(mine - a[key]) > 0.02 + 0.001 * abs(mine):
                drift.append((a["op"], a["threads"], key, a[key], round(mine, 4)))
        mine_aps = a["attempts"] / a["ops"]
        if abs(mine_aps - a["attempts_per_success"]) > 1e-3:
            drift.append((a["op"], a["threads"], "attempts_per_success",
                          a["attempts_per_success"], round(mine_aps, 4)))
    r(not drift, "R13 reported rates match elapsed/count", drift[:4] or "all 6 arms agree")

    al = d.get("alignment", [])
    r([a["case"] for a in al] == list(ALIGN), "R14 alignment case coverage",
      f"{len(al)} cases")
    ctl = {a["case"]: a for a in al}
    r(not ctl[ALIGN[0]]["faulted"], "R15 aligned control does not fault", ctl[ALIGN[0]])
    r(not ctl[ALIGN[4]]["faulted"], "R16 ordinary load does not fault", ctl[ALIGN[4]])
    r(not [a for a in al if a["faulted"] and a["signal"] == 0],
      "R17 every fault carries a signal", "ok")

    rt = d.get("roundtrip", [])
    r({x["qos"] for x in rt} == set(QOS) and len(rt) == 2, "R18 round-trip coverage",
      [x["qos"] for x in rt])
    r(not [x for x in rt if x["timeouts"]], "R19 no round-trip timeouts",
      [(x["qos"], x["timeouts"]) for x in rt])
    r(all(0 < x["min_ns"] <= x["p50_ns"] <= x["p99_ns"] <= x["max_ns"] for x in rt),
      "R20 round-trip percentiles ordered and positive",
      [(x["qos"], x["min_ns"], x["p50_ns"], x["p99_ns"], x["max_ns"]) for x in rt])

    print(f"INV-01-08 independent verifier (REV-086)  file={path}")
    for line in oks + fails:
        print("  " + line)

    print("  -- histogram adjudication, recomputed --")
    def both(l):
        h = l.get("histogram") or {}
        return h.get(BOTH_CELL, 0) / (sum(h.values()) or 1)
    for t in TESTS:
        for q in QOS:
            p = [l for l in lit if (l["test"], l["variant"], l["qos"]) == (t, "plain-ldr-str", q)]
            if not p:
                continue
            fp = both(p[0])
            for v in ("ldar-stlr", "dmb-ish"):
                s = [l for l in lit if (l["test"], l["variant"], l["qos"]) == (t, v, q)]
                if not s:
                    continue
                fs = both(s[0])
                lo, hi = sorted((fp, fs))
                verdict = "COMPARABLE" if hi <= max(lo, 1e-9) * 10 else "INCONCLUSIVE"
                print(f"  {verdict}  {t}/{v}/{q}: plain={fp:.6f} strengthened={fs:.6f}")

    print("  -- thresholds, recomputed (reported only) --")
    a16 = ctl["ldaddal crossing 16-byte boundary"]
    print(f"  T1 cross-16B atomic faults: {a16['faulted']} "
          f"signal={a16['signal']} si_code={a16['si_code']}")
    fb = d.get("fallback_global_lock", {})
    print(f"  T2 fallback {fb['elapsed_ns'] / fb['ops']:.2f} ns/op (bound 500) "
          f"[reported {fb['ns_per_op']}]")
    ui = [x for x in rt if x["qos"] == "user-interactive"][0]
    print(f"  T3 round trip p99 {ui['p99_ns']} ns (bound 50000)")
    g = lambda o, n, k: [x for x in lse if x["op"] == o and x["threads"] == n][0][k]
    for o in ("ldadd", "cas"):
        print(f"  T4a {o} ns/attempt {g(o,2,'ns_per_attempt'):.2f} -> "
              f"{g(o,8,'ns_per_attempt'):.2f} = "
              f"{g(o,8,'ns_per_attempt')/g(o,2,'ns_per_attempt'):.2f}x (bound 20)")
    print(f"  T4b cas ns/op {g('cas',2,'ns_per_op'):.2f} -> {g('cas',8,'ns_per_op'):.2f} = "
          f"{g('cas',8,'ns_per_op')/g('cas',2,'ns_per_op'):.2f}x (bound 60)")

    print(f"VERIFY: {'FAIL' if fails else 'PASS'} ({len(fails)} invariant failures)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "result.json"))

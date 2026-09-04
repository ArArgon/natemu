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
import json
import sys

MIN_ITERS = 1000000
QOS = {"user-interactive", "background"}
LITMUS_CASES = {
    (test, variant, qos)
    for test in ("SB", "MP")
    for variant in ("plain-ldr-str", "ldar-stlr", "dmb-ish")
    for qos in QOS
}
LSE_CASES = {(op, threads) for op in ("ldadd", "cas") for threads in (2, 4, 8)}
ALIGNMENT_CASES = {
    "ldaddal aligned 4B (control)",
    "ldaddal misaligned within 16B granule",
    "ldaddal crossing 16-byte boundary",
    "ldaddal crossing 64-byte cache line",
    "plain ldr crossing 64-byte cache line",
    "ldar crossing 64-byte cache line",
}


def main(path):
    d = json.load(open(path))
    fails, notes = [], []

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
        if l["relaxed_observed"] > 0
        and ((l["test"] == "SB" and l["variant"] == "dmb-ish")
             or (l["test"] == "MP" and l["variant"] in ("ldar-stlr", "dmb-ish")))
    ]
    rule(not forbidden,
         "I5 no architecture-forbidden relaxed outcome",
         f"forbidden SB/DMB or MP/acquire-release-or-DMB outcomes: {forbidden[:4]}")

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

    print("  -- decision thresholds (result gate adjudicates) --")

    def thr(name, ok, detail):
        print(f"  {'MEET' if ok else 'MISS'}  {name}: {detail}")

    cross16 = [a for a in al if "crossing 16-byte boundary" in a["case"]]
    if cross16:
        a = cross16[0]
        thr("T1 cross-16-byte atomic faults on this host",
            a["faulted"],
            f"faulted={a['faulted']} signal={a['signal']} si_code={a['si_code']} "
            "(categorical host outcome; a completion is not evidence of non-atomicity)")

    fb = d.get("fallback_global_lock", {})
    thr("T2 software fallback under 500 ns/op at 4 threads",
        fb.get("ns_per_op", 1e9) < 500, f"{fb.get('ns_per_op')} ns/op")

    ui = [r for r in rt if r["qos"] == "user-interactive"]
    thr("T3 cross-thread invalidation round trip p99 < 50 us",
        ui and ui[0]["p99_ns"] < 50000,
        f"{[(r['qos'], r['p50_ns'], r['p99_ns']) for r in rt]}")

    lse8 = [a for a in d.get("lse", []) if a["threads"] == 8]
    lse2 = [a for a in d.get("lse", []) if a["threads"] == 2]
    if lse8 and lse2:
        for op in ("ldadd", "cas"):
            a8 = [x for x in lse8 if x["op"] == op]
            a2 = [x for x in lse2 if x["op"] == op]
            if a8 and a2:
                thr(f"T4 {op} 8-thread/2-thread cost ratio < 20x",
                    a8[0]["ns_per_op"] / max(a2[0]["ns_per_op"], 1e-9) < 20,
                    f"{a2[0]['ns_per_op']:.2f} -> {a8[0]['ns_per_op']:.2f} ns/op")

    print(f"RESULT: {'FAIL' if fails else 'PASS'} ({len(fails)} integrity failures)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "result.json"))

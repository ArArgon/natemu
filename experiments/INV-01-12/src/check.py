#!/usr/bin/env python3
"""Invariant checker for the INV-01-12 integrated prototype.

Reads the prototype's JSON output and evaluates the invariants predeclared in
docs/research/investigations/INV-01-12.md. Prints PASS or FAIL with the list of
failed invariants; exits 0 on PASS and 1 on FAIL.

  check.py <result.json>
  check.py --selftest        run the checker's own self-check

Every negative control is expected to make this checker report FAIL. The
invariant each control is predicted to break is named in the method; a control
that produces PASS means the checker is not an oracle for that invariant.
Python 3 standard library only.
"""

import json
import sys

ALIAS = 0x40000000
FAULT_CODES = [0, 2, 3]          # not-present read, not-present write, write to read-only


def check(d):
    """Return (list of failed invariant descriptions, list of notes)."""
    bad, notes = [], []

    def req(cond, desc):
        if not cond:
            bad.append(desc)

    g = d["guest"]
    h = d["host"]
    v = d["vcpu"]
    log = g["fault_log"]

    # I1 the run completed under its own control
    req(d["abort"] is None, "I1a run aborted: %s" % d["abort"])
    req(d["timed_out"] is False, "I1b run hit the watchdog timeout")

    # I2 both vCPUs came up, finished, and halted cleanly
    req(len(v) == 2, "I2a expected two vCPUs, got %d" % len(v))
    for c in v:
        req(c["halted_clean"], "I2b vCPU %d did not halt cleanly" % c["id"])
        req(c["finished"], "I2c vCPU %d did not signal completion" % c["id"])
        req(c["translations"] >= 1, "I2d vCPU %d translated no block" % c["id"])
        req(c["tlb_fills"] >= 1, "I2e vCPU %d filled no TLB entry" % c["id"])
    req(g["vcpu0_done"] == 1 and g["vcpu1_done"] == 1, "I2f a vCPU did not record completion")

    # I3 atomic read-modify-write under real contention
    req(g["counter"] == 2 * g["loop_k"],
        "I3 shared counter is %d, expected %d" % (g["counter"], 2 * g["loop_k"]))

    # I4 message passing: no store-store or load-load reordering observed
    req(g["mp_violations"] == 0, "I4a %d message-passing violations" % g["mp_violations"])
    req(g["mp_timeouts"] == 0, "I4b %d message-passing spin timeouts" % g["mp_timeouts"])
    req(g["iters0"] == g["loop_n"] and g["iters1"] == g["loop_n"],
        "I4c message-passing iteration counts %d/%d, expected %d/%d"
        % (g["iters0"], g["iters1"], g["loop_n"], g["loop_n"]))

    # I5 page faults: count, discrimination, CR2, and the pushed RIP
    req(g["pf_count"] == 3, "I5a %d page faults, expected 3" % g["pf_count"])
    for i in range(3):
        req(log[i]["err"] == FAULT_CODES[i],
            "I5b fault %d error code %d, expected %d" % (i, log[i]["err"], FAULT_CODES[i]))
        req(log[i]["cr2"] == ALIAS + 0x1000 * i,
            "I5c fault %d CR2 0x%x, expected 0x%x" % (i, log[i]["cr2"], ALIAS + 0x1000 * i))
        req(log[i]["expected_rip"] != 0 and log[i]["pushed_rip"] == log[i]["expected_rip"],
            "I5d fault %d pushed RIP 0x%x, expected 0x%x"
            % (i, log[i]["pushed_rip"], log[i]["expected_rip"]))

    # I6 restart is exactly once: the faulting instruction's effect applied once
    req(g["test_a_val"] == 0xA5A5A5A5,
        "I6a restarted load read 0x%x, expected 0xa5a5a5a5" % g["test_a_val"])
    req(g["test_b_mem"] == 1,
        "I6b restarted not-present write left %d, expected 1" % g["test_b_mem"])
    req(g["test_c_mem"] == 1,
        "I6c restarted read-only write left %d, expected 1" % g["test_c_mem"])

    # I7 #UD delivered and resumed; no #GP anywhere
    req(g["ud_count"] == 1, "I7a %d #UD deliveries, expected 1" % g["ud_count"])
    req(g["gp_count"] == 0, "I7b %d #GP deliveries, expected 0" % g["gp_count"])

    # I8 self-modifying code detected and retranslated
    req(g["smc_before"] == 111, "I8a pre-patch code returned %d, expected 111" % g["smc_before"])
    req(g["smc_after"] == 222, "I8b post-patch code returned %d, expected 222" % g["smc_after"])
    req(sum(c["smc_flushes"] for c in v) >= 1, "I8c no code-cache flush recorded")

    # I9 timer interrupts, under the post-on-EOI discipline
    req(h["timer_posted"] - g["timer_ticks"] == h["timer_pending_at_end"],
        "I9a host posted %d ticks, guest saw %d, %d pending"
        % (h["timer_posted"], g["timer_ticks"], h["timer_pending_at_end"]))
    req(g["timer_ticks"] == g["timer_eois"],
        "I9b %d ticks but %d EOIs" % (g["timer_ticks"], g["timer_eois"]))
    req(g["timer_ticks"] >= 5, "I9c only %d timer ticks; too few to test" % g["timer_ticks"])

    # I10 IF gates delivery, and HLT wakes on an interrupt
    req(g["cli_delta"] == 0, "I10a %d ticks delivered inside the cli region" % g["cli_delta"])
    req(g["hlt_wakeups"] >= 1, "I10b guest recorded no wake from HLT")
    req(sum(c["hlt_wakes"] for c in v) >= 1, "I10c host recorded no wake from HLT")

    # I11 inter-processor interrupts
    req(h["ipi_sent"] == g["ipi_recv"],
        "I11a %d IPIs sent, %d received" % (h["ipi_sent"], g["ipi_recv"]))
    req(h["ipi_dropped"] == 0, "I11b %d IPIs dropped" % h["ipi_dropped"])
    req(g["ipi_ack_timeouts"] == 0, "I11c %d IPI acknowledgements timed out" % g["ipi_ack_timeouts"])
    req(h["ipi_sent"] >= 1, "I11d no IPI was sent")

    # I12 compare-and-exchange succeeds then fails, loading the old value
    req(g["cas_ok"] == 1, "I12a compare-and-exchange did not succeed when it should")
    req(g["cas_fail_rax"] == 1, "I12b failing compare-and-exchange left %d in RAX, expected 1"
        % g["cas_fail_rax"])

    # I13 the two vCPUs' execution windows overlap on one host clock
    if len(v) == 2 and all(c["mark_begin_ns"] and c["mark_end_ns"] for c in v):
        lo = max(c["mark_begin_ns"] for c in v)
        hi = min(c["mark_end_ns"] for c in v)
        overlap = hi - lo
        notes.append("overlapping execution window: %d ns" % overlap)
        req(overlap > 0, "I13 execution windows do not overlap (%d ns)" % overlap)
    else:
        bad.append("I13 an execution-window mark is missing")

    # I14 faults were raised from inside translated code
    req(v[0]["faults_raised"] >= 4, "I14 vCPU 0 raised %d exceptions, expected at least 4"
        % v[0]["faults_raised"])

    return bad, notes


def report(d):
    bad, notes = check(d)
    for n in notes:
        print("note: %s" % n)
    if d.get("injection", "none") != "none":
        print("note: injection %r active; this run is a negative control and is "
              "predicted to FAIL" % d["injection"])
        if d["injection"] == "no-barrier":
            print("note: the no-barrier control is stochastic; a PASS here is not "
                  "evidence that the acquire/release emission is unnecessary")
    if bad:
        print("FAIL (%d invariant%s)" % (len(bad), "" if len(bad) == 1 else "s"))
        for b in bad:
            print("  - %s" % b)
        return 1
    print("PASS")
    return 0


def selftest():
    """The smallest check that fails if the invariant logic breaks."""
    good = {
        "injection": "none", "abort": None, "timed_out": False,
        "host": {"timer_posted": 10, "timer_pending_at_end": 0, "ipi_sent": 8, "ipi_dropped": 0},
        "vcpu": [
            {"id": 0, "translations": 5, "tlb_fills": 5, "smc_flushes": 1, "faults_raised": 4,
             "hlt_wakes": 1, "halted_clean": True, "finished": True,
             "mark_begin_ns": 100, "mark_end_ns": 300},
            {"id": 1, "translations": 5, "tlb_fills": 5, "smc_flushes": 0, "faults_raised": 0,
             "hlt_wakes": 0, "halted_clean": True, "finished": True,
             "mark_begin_ns": 120, "mark_end_ns": 320},
        ],
        "guest": {
            "counter": 200000, "loop_k": 100000, "loop_n": 4096,
            "mp_violations": 0, "mp_timeouts": 0, "iters0": 4096, "iters1": 4096,
            "pf_count": 3, "ud_count": 1, "gp_count": 0,
            "timer_ticks": 10, "timer_eois": 10, "ipi_recv": 8, "ipi_ack_timeouts": 0,
            "hlt_wakeups": 1, "cli_delta": 0, "smc_before": 111, "smc_after": 222,
            "cas_ok": 1, "cas_fail_rax": 1, "test_a_val": 0xA5A5A5A5,
            "test_b_mem": 1, "test_c_mem": 1, "vcpu0_done": 1, "vcpu1_done": 1,
            "fault_log": [{"cr2": ALIAS + 0x1000 * i, "err": FAULT_CODES[i],
                           "pushed_rip": 100 + i, "expected_rip": 100 + i} for i in range(3)]
            + [{"cr2": 0, "err": 0, "pushed_rip": 0, "expected_rip": 0}],
        },
    }
    assert check(good)[0] == [], check(good)[0]

    # each mutation must be caught by exactly the invariant that owns it
    mutations = [
        ("counter", 199999, "I3"), ("mp_violations", 1, "I4a"), ("test_b_mem", 0, "I6b"),
        ("test_a_val", 0, "I6a"), ("smc_after", 111, "I8b"), ("ipi_recv", 7, "I11a"),
        ("timer_eois", 9, "I9b"), ("cli_delta", 1, "I10a"), ("ud_count", 0, "I7a"),
        ("gp_count", 1, "I7b"), ("cas_fail_rax", 0, "I12b"), ("pf_count", 2, "I5a"),
    ]
    for key, val, tag in mutations:
        d = json.loads(json.dumps(good))
        d["guest"][key] = val
        bad = check(d)[0]
        assert any(b.startswith(tag) for b in bad), (key, bad)

    d = json.loads(json.dumps(good))
    d["guest"]["fault_log"][1]["err"] = 3
    assert any(b.startswith("I5b") for b in check(d)[0])

    d = json.loads(json.dumps(good))
    d["guest"]["fault_log"][0]["pushed_rip"] += 5      # a skipped restart
    assert any(b.startswith("I5d") for b in check(d)[0])

    d = json.loads(json.dumps(good))
    d["vcpu"][1]["mark_begin_ns"] = 400               # no overlap
    d["vcpu"][1]["mark_end_ns"] = 600
    assert any(b.startswith("I13") for b in check(d)[0])

    d = json.loads(json.dumps(good))
    d["timed_out"] = True
    assert any(b.startswith("I1b") for b in check(d)[0])

    print("selftest OK")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        sys.exit(selftest())
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(report(json.load(open(sys.argv[1]))))

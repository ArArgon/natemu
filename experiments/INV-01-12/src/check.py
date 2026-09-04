#!/usr/bin/env python3
"""Invariant checker for the INV-01-12 integrated prototype.

Reads the prototype's JSON output and evaluates the invariants predeclared in
docs/research/investigations/INV-01-12.md. Prints PASS or FAIL with the list of
failed invariants; exits 0 on PASS and 1 on FAIL.

  check.py <result.json>
  check.py <result.json> --expect I6d,I6e    require those invariants to fail
  check.py --selftest                        run the checker's own self-check

Every negative control is expected to make this checker report FAIL. The
invariant each control is predicted to break is named in the method; a control
that produces PASS means the checker is not an oracle for that invariant.
Python 3 standard library only.
"""

import json
import sys

# Predeclared configuration. These are asserted, not read out of the run: a
# document that reports a different build is not the frozen experiment.
LOOP_K = 100000
LOOP_N = 4096
NVCPU = 2
TIMER_PERIOD_NS = 1000000
PARALLEL_MIN = 1.5           # (sum of per-thread CPU time) / wall span

ALIAS = 0x40000000
FAULT_CODES = [0, 2, 3, 2]   # not-present read, not-present write, write to
                             # read-only, not-present write from a stack push
FAULT_CR2 = [ALIAS, ALIAS + 0x1000, ALIAS + 0x2000, ALIAS + 0x3FF8]
PUSH_RSP_AFTER = 0x40003FF8
PUSH_VAL = 0x5A5A5A5A
AD_ACCESSED = 0x20
AD_ACCESSED_DIRTY = 0x60


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
    for c in v:
        req(not c["nested_fault"], "I1c vCPU %d took a nested fault" % c["id"])

    # I2 both vCPUs came up, finished, and halted cleanly
    req(len(v) == NVCPU, "I2a expected %d vCPUs, got %d" % (NVCPU, len(v)))
    for c in v:
        req(c["halted_clean"], "I2b vCPU %d did not halt cleanly" % c["id"])
        req(c["finished"], "I2c vCPU %d did not signal completion" % c["id"])
        req(c["translations"] >= 1, "I2d vCPU %d translated no block" % c["id"])
        req(c["tlb_fills"] >= 1, "I2e vCPU %d filled no TLB entry" % c["id"])
        # I2g the code cache was actually used, which Alternative 5 relies on
        req(c["blocks"] > 2 * c["translations"],
            "I2g vCPU %d executed %d blocks from %d translations; the code cache "
            "was not reused" % (c["id"], c["blocks"], c["translations"]))
    req(g["vcpu0_done"] == 1 and g["vcpu1_done"] == 1, "I2f a vCPU did not record completion")

    # I2h the run is the frozen configuration, asserted rather than reported
    req(d["jit_mapping"] == "MAP_JIT",
        "I2h translated code was not in a MAP_JIT region: %r" % d["jit_mapping"])
    req(d.get("injection", "none") == "none",
        "I2i a defect injection was active: %r" % d.get("injection"))
    req(g["loop_k"] == LOOP_K, "I2j loop_k is %d, frozen at %d" % (g["loop_k"], LOOP_K))
    req(g["loop_n"] == LOOP_N, "I2k loop_n is %d, frozen at %d" % (g["loop_n"], LOOP_N))
    req(d.get("nvcpu") == NVCPU, "I2l nvcpu is %r, frozen at %d" % (d.get("nvcpu"), NVCPU))
    req(d.get("timer_period_ns") == TIMER_PERIOD_NS,
        "I2m timer period is %r ns, frozen at %d" % (d.get("timer_period_ns"), TIMER_PERIOD_NS))

    # I3 atomic read-modify-write under real contention
    req(g["counter"] == NVCPU * LOOP_K,
        "I3 shared counter is %d, expected %d" % (g["counter"], NVCPU * LOOP_K))

    # I4 message passing: no store-store or load-load reordering observed
    req(g["mp_violations"] == 0, "I4a %d message-passing violations" % g["mp_violations"])
    req(g["mp_timeouts"] == 0, "I4b %d message-passing spin timeouts" % g["mp_timeouts"])
    req(g["iters0"] == LOOP_N and g["iters1"] == LOOP_N,
        "I4c message-passing iteration counts %d/%d, expected %d/%d"
        % (g["iters0"], g["iters1"], LOOP_N, LOOP_N))

    # I5 page faults: count, discrimination, CR2, and the pushed RIP
    req(g["pf_count"] == 4, "I5a %d page faults, expected 4" % g["pf_count"])
    for i in range(4):
        req(log[i]["err"] == FAULT_CODES[i],
            "I5b fault %d error code %d, expected %d" % (i, log[i]["err"], FAULT_CODES[i]))
        req(log[i]["cr2"] == FAULT_CR2[i],
            "I5c fault %d CR2 0x%x, expected 0x%x" % (i, log[i]["cr2"], FAULT_CR2[i]))
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
    req(g["push_rsp_after"] == PUSH_RSP_AFTER,
        "I6d restarted push left RSP at 0x%x, expected 0x%x; the stack pointer was "
        "committed more than once" % (g["push_rsp_after"], PUSH_RSP_AFTER))
    req(g["push_val"] == PUSH_VAL,
        "I6e restarted push stored 0x%x at 0x%x, expected 0x%x"
        % (g["push_val"], PUSH_RSP_AFTER, PUSH_VAL))

    # I7 #UD delivered and resumed; no #GP anywhere
    req(g["ud_count"] == 1, "I7a %d #UD deliveries, expected 1" % g["ud_count"])
    req(g["gp_count"] == 0, "I7b %d #GP deliveries, expected 0" % g["gp_count"])

    # I8 self-modifying code detected and retranslated by the storing vCPU
    req(g["smc_before"] == 111, "I8a pre-patch code returned %d, expected 111" % g["smc_before"])
    req(g["smc_after"] == 222, "I8b post-patch code returned %d, expected 222" % g["smc_after"])
    req(v[0]["smc_flushes"] >= 1,
        "I8c vCPU 0 performed the patch but recorded %d code-cache flushes"
        % v[0]["smc_flushes"])

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

    # I13 the two vCPU threads ran in parallel, not time-sliced on one core.
    # The overlap of the marked windows only shows both threads were alive; the
    # ratio of consumed thread CPU time to the wall span is what separates two
    # cores running at once from one core interleaving them.
    if len(v) == NVCPU and all(c["mark_begin_ns"] and c["mark_end_ns"] for c in v):
        overlap = min(c["mark_end_ns"] for c in v) - max(c["mark_begin_ns"] for c in v)
        span = max(c["mark_end_ns"] for c in v) - min(c["mark_begin_ns"] for c in v)
        cpu_sum = sum(c.get("cpu_ns", 0) for c in v)
        ratio = cpu_sum / span if span else 0.0
        notes.append("overlap %d ns; wall span %d ns; thread CPU %d ns; parallelism %.2fx"
                     % (overlap, span, cpu_sum, ratio))
        req(overlap > 0, "I13a execution windows do not overlap (%d ns)" % overlap)
        req(ratio >= PARALLEL_MIN,
            "I13b parallelism %.2fx over the concurrent phase, below the predeclared "
            "floor of %.2fx; the two vCPU threads may have been time-sliced on one core"
            % (ratio, PARALLEL_MIN))
    else:
        bad.append("I13a an execution-window mark is missing")

    # I14 faults were raised from inside translated code
    req(v[0]["faults_raised"] >= 5, "I14 vCPU 0 raised %d exceptions, expected at least 5"
        % v[0]["faults_raised"])

    # I15 the software MMU raised accessed on every touched page and dirty only
    # on the written ones, observed by the guest reading back its own PTEs
    req(g["ad_a"] == AD_ACCESSED,
        "I15a read-faulted page has A/D bits 0x%x, expected 0x%x (accessed, not dirty)"
        % (g["ad_a"], AD_ACCESSED))
    req(g["ad_b"] == AD_ACCESSED_DIRTY,
        "I15b write-faulted page has A/D bits 0x%x, expected 0x%x" % (g["ad_b"], AD_ACCESSED_DIRTY))
    req(g["ad_c"] == AD_ACCESSED_DIRTY,
        "I15c read-only-faulted page has A/D bits 0x%x, expected 0x%x"
        % (g["ad_c"], AD_ACCESSED_DIRTY))

    return bad, notes


def report(d, expect=()):
    bad, notes = check(d)
    for n in notes:
        print("note: %s" % n)
    if d.get("injection", "none") != "none":
        print("note: injection %r active; this run is a negative control and is "
              "predicted to FAIL" % d["injection"])
        if d["injection"] == "no-barrier":
            print("note: the no-barrier control is stochastic; a PASS here is not "
                  "evidence that the acquire/release emission is unnecessary")
    missing = [t for t in expect if not any(b.startswith(t) for b in bad)]
    if expect:
        # Separates the invariant a control targets from the collateral damage
        # its defect causes downstream; only the targeted ones are witnesses.
        print("note: targeted invariants %s; collateral failures are not witnesses"
              % ", ".join(expect))
    if bad:
        print("FAIL (%d invariant%s)" % (len(bad), "" if len(bad) == 1 else "s"))
        for b in bad:
            print("  - %s%s" % (b, "   [targeted]" if any(b.startswith(t) for t in expect) else ""))
    else:
        print("PASS")
    if missing:
        print("UNWITNESSED: expected invariant(s) did not fail: %s" % ", ".join(missing))
        return 1
    return 1 if bad else 0


def selftest():
    """The smallest check that fails if the invariant logic breaks.

    Every assertion the checker makes is either broken by a runtime negative
    control or by a mutation here; the mapping is tabulated in the method's
    witness table.
    """
    good = {
        "injection": "none", "abort": None, "timed_out": False,
        "jit_mapping": "MAP_JIT", "nvcpu": NVCPU, "timer_period_ns": TIMER_PERIOD_NS,
        "host": {"timer_posted": 10, "timer_pending_at_end": 0, "ipi_sent": 8, "ipi_dropped": 0},
        "vcpu": [
            {"id": 0, "translations": 5, "blocks": 500, "tlb_fills": 5, "smc_flushes": 1,
             "faults_raised": 5, "hlt_wakes": 1, "halted_clean": True, "finished": True,
             "nested_fault": False, "mark_begin_ns": 100, "mark_end_ns": 300, "cpu_ns": 195},
            {"id": 1, "translations": 5, "blocks": 500, "tlb_fills": 5, "smc_flushes": 0,
             "faults_raised": 0, "hlt_wakes": 0, "halted_clean": True, "finished": True,
             "nested_fault": False, "mark_begin_ns": 120, "mark_end_ns": 320, "cpu_ns": 195},
        ],
        "guest": {
            "counter": NVCPU * LOOP_K, "loop_k": LOOP_K, "loop_n": LOOP_N,
            "mp_violations": 0, "mp_timeouts": 0, "iters0": LOOP_N, "iters1": LOOP_N,
            "pf_count": 4, "ud_count": 1, "gp_count": 0,
            "timer_ticks": 10, "timer_eois": 10, "ipi_recv": 8, "ipi_ack_timeouts": 0,
            "hlt_wakeups": 1, "cli_delta": 0, "smc_before": 111, "smc_after": 222,
            "cas_ok": 1, "cas_fail_rax": 1, "test_a_val": 0xA5A5A5A5,
            "test_b_mem": 1, "test_c_mem": 1, "vcpu0_done": 1, "vcpu1_done": 1,
            "push_rsp_after": PUSH_RSP_AFTER, "push_val": PUSH_VAL,
            "ad_a": AD_ACCESSED, "ad_b": AD_ACCESSED_DIRTY, "ad_c": AD_ACCESSED_DIRTY,
            "fault_log": [{"cr2": FAULT_CR2[i], "err": FAULT_CODES[i],
                           "pushed_rip": 100 + i, "expected_rip": 100 + i} for i in range(4)],
        },
    }
    assert check(good)[0] == [], check(good)[0]

    def mutated(**path):
        return json.loads(json.dumps(good))

    # guest-field mutations, each caught by the invariant that owns it
    for key, val, tag in [
        ("counter", NVCPU * LOOP_K - 1, "I3"), ("mp_violations", 1, "I4a"),
        ("mp_timeouts", 1, "I4b"), ("iters0", 1, "I4c"),
        ("test_b_mem", 0, "I6b"), ("test_a_val", 0, "I6a"), ("test_c_mem", 0, "I6c"),
        ("push_rsp_after", PUSH_RSP_AFTER - 8, "I6d"), ("push_val", 0, "I6e"),
        ("smc_after", 111, "I8b"), ("smc_before", 0, "I8a"),
        ("ipi_recv", 7, "I11a"), ("ipi_ack_timeouts", 1, "I11c"),
        ("timer_eois", 9, "I9b"), ("timer_ticks", 1, "I9c"),
        ("cli_delta", 1, "I10a"), ("hlt_wakeups", 0, "I10b"),
        ("ud_count", 0, "I7a"), ("gp_count", 1, "I7b"),
        ("cas_fail_rax", 0, "I12b"), ("cas_ok", 0, "I12a"),
        ("pf_count", 3, "I5a"), ("vcpu0_done", 0, "I2f"),
        ("loop_k", 1, "I2j"), ("loop_n", 1, "I2k"),
        ("ad_a", AD_ACCESSED_DIRTY, "I15a"), ("ad_b", AD_ACCESSED, "I15b"),
        ("ad_c", AD_ACCESSED, "I15c"),
    ]:
        d = mutated()
        d["guest"][key] = val
        assert any(b.startswith(tag) for b in check(d)[0]), (key, check(d)[0])

    # top-level and per-vCPU mutations
    for setter, tag in [
        (lambda d: d.update(timed_out=True), "I1b"),
        (lambda d: d.update(abort="boom"), "I1a"),
        (lambda d: d.update(jit_mapping="MAP_JIT failed; retried without it"), "I2h"),
        (lambda d: d.update(injection="skip-restart"), "I2i"),
        (lambda d: d.update(nvcpu=1), "I2l"),
        (lambda d: d.update(timer_period_ns=1), "I2m"),
        (lambda d: d["host"].update(ipi_dropped=1), "I11b"),
        (lambda d: d["host"].update(ipi_sent=0), "I11a"),
        (lambda d: d["host"].update(timer_posted=11), "I9a"),
        (lambda d: d["vcpu"][0].update(nested_fault=True), "I1c"),
        (lambda d: d["vcpu"][1].update(halted_clean=False), "I2b"),
        (lambda d: d["vcpu"][1].update(finished=False), "I2c"),
        (lambda d: d["vcpu"][1].update(translations=0), "I2d"),
        (lambda d: d["vcpu"][1].update(tlb_fills=0), "I2e"),
        (lambda d: d["vcpu"][0].update(blocks=5), "I2g"),          # no code cache
        (lambda d: d["vcpu"][0].update(smc_flushes=0, ), "I8c"),   # flush on the wrong vCPU
        (lambda d: d["vcpu"][0].update(faults_raised=0), "I14"),
        (lambda d: d["vcpu"][0].update(hlt_wakes=0), "I10c"),
        (lambda d: d["vcpu"][1].update(mark_begin_ns=400, mark_end_ns=600), "I13a"),
        (lambda d: (d["vcpu"][0].update(cpu_ns=90), d["vcpu"][1].update(cpu_ns=90)), "I13b"),
        (lambda d: d["guest"]["fault_log"][1].update(err=3), "I5b"),
        (lambda d: d["guest"]["fault_log"][2].update(cr2=0), "I5c"),
        (lambda d: d["guest"]["fault_log"][3].update(cr2=0), "I5c"),
        (lambda d: d["guest"]["fault_log"][0].update(pushed_rip=999), "I5d"),
    ]:
        d = mutated()
        setter(d)
        assert any(b.startswith(tag) for b in check(d)[0]), (tag, check(d)[0])

    print("selftest OK")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        sys.exit(selftest())
    expect = ()
    if len(sys.argv) == 4 and sys.argv[2] == "--expect":
        expect = tuple(t for t in sys.argv[3].split(",") if t)
    elif len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(report(json.load(open(sys.argv[1])), expect))

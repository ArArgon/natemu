#!/usr/bin/env python3
"""Independent invariant checker for INV-01-12, written for REV-102.

This is NOT derived from experiments/INV-01-12/src/check.py. Every assertion
below was written from the method of record — the Metrics table of
docs/research/investigations/INV-01-12.md and the predicted-outcome cells of
CLM-01-12-04..CLM-01-12-16 — so that a defect shared with the subject checker
cannot hide in both. It is deliberately a flat table of assertions rather than
an imperative sequence, and it recomputes the derived quantities (the
post-on-EOI identity, the overlap, the parallelism ratio) from the raw fields
instead of trusting any figure the subject checker printed.

  recheck.py <result.json> [<result.json> ...]   evaluate each document
  recheck.py --selftest                          this checker's own check

Exit 0 if every named document satisfies every assertion, 1 otherwise.
Python 3 standard library only.
"""

import json
import sys

# Frozen configuration and thresholds, read off the method's Metrics table.
FROZEN = dict(jit_mapping="MAP_JIT", injection="none", nvcpu=2,
              loop_k=100000, loop_n=4096, timer_period_ns=1000000)
ALIAS = 0x40000000
WANT_ERR = [0, 2, 3, 2]
WANT_CR2 = [ALIAS, ALIAS + 0x1000, ALIAS + 0x2000, ALIAS + 0x3FF8]
PARALLEL_FLOOR = 1.50


def assertions(d):
    """Yield (name, ok, detail) for every predeclared threshold."""
    g, h, v = d.get("guest", {}), d.get("host", {}), d.get("vcpu", [])
    log = g.get("fault_log", [])
    K, N = FROZEN["loop_k"], FROZEN["loop_n"]

    yield "run completed", d.get("abort") is None, "abort=%r" % d.get("abort")
    yield "no watchdog timeout", d.get("timed_out") is False, "timed_out=%r" % d.get("timed_out")
    yield "two vCPUs present", len(v) == FROZEN["nvcpu"], "len(vcpu)=%d" % len(v)
    if len(v) != FROZEN["nvcpu"]:
        return                                    # nothing below is evaluable

    # frozen configuration, asserted against constants rather than the document
    for k, want in FROZEN.items():
        got = d.get(k) if k in ("jit_mapping", "injection", "nvcpu", "timer_period_ns") else g.get(k)
        yield "frozen %s" % k, got == want, "got %r want %r" % (got, want)

    # liveness and completion
    for c in v:
        i = c["id"]
        yield "vCPU%d halted clean" % i, c["halted_clean"] is True, ""
        yield "vCPU%d finished" % i, c["finished"] is True, ""
        yield "vCPU%d no nested fault" % i, c["nested_fault"] is False, ""
        yield "vCPU%d translated" % i, c["translations"] >= 1, "translations=%d" % c["translations"]
        yield "vCPU%d filled TLB" % i, c["tlb_fills"] >= 1, "tlb_fills=%d" % c["tlb_fills"]
        yield "vCPU%d reused code cache" % i, c["blocks"] > 2 * c["translations"], \
            "blocks=%d translations=%d" % (c["blocks"], c["translations"])
    yield "both vCPUs recorded done", g["vcpu0_done"] == 1 and g["vcpu1_done"] == 1, ""

    # atomics and ordering under concurrency
    yield "shared counter exact", g["counter"] == 2 * K, "counter=%d want %d" % (g["counter"], 2 * K)
    yield "no message-passing violation", g["mp_violations"] == 0, "mp_violations=%d" % g["mp_violations"]
    yield "no message-passing timeout", g["mp_timeouts"] == 0, "mp_timeouts=%d" % g["mp_timeouts"]
    yield "message-passing iterations", g["iters0"] == N and g["iters1"] == N, \
        "iters=%d/%d want %d" % (g["iters0"], g["iters1"], N)

    # faults: count, discrimination, CR2, pushed RIP
    yield "fault count", g["pf_count"] == 4, "pf_count=%d" % g["pf_count"]
    for i in range(4):
        f = log[i] if i < len(log) else {}
        yield "fault%d error code" % i, f.get("err") == WANT_ERR[i], \
            "err=%r want %d" % (f.get("err"), WANT_ERR[i])
        yield "fault%d CR2" % i, f.get("cr2") == WANT_CR2[i], \
            "cr2=%#x want %#x" % (f.get("cr2", 0), WANT_CR2[i])
        yield "fault%d pushed RIP" % i, f.get("expected_rip", 0) != 0 and \
            f.get("pushed_rip") == f.get("expected_rip"), \
            "pushed=%#x expected=%#x" % (f.get("pushed_rip", 0), f.get("expected_rip", 0))

    # restart exactly once, including the stack pointer
    yield "restarted load value", g["test_a_val"] == 0xA5A5A5A5, "%#x" % g["test_a_val"]
    yield "restarted np write once", g["test_b_mem"] == 1, "test_b_mem=%d" % g["test_b_mem"]
    yield "restarted ro write once", g["test_c_mem"] == 1, "test_c_mem=%d" % g["test_c_mem"]
    yield "restarted push RSP", g["push_rsp_after"] == ALIAS + 0x3FF8, "%#x" % g["push_rsp_after"]
    yield "restarted push value", g["push_val"] == 0x5A5A5A5A, "%#x" % g["push_val"]

    yield "one #UD", g["ud_count"] == 1, "ud_count=%d" % g["ud_count"]
    yield "no #GP", g["gp_count"] == 0, "gp_count=%d" % g["gp_count"]
    yield "faults raised in translated code", v[0]["faults_raised"] >= 5, \
        "faults_raised=%d" % v[0]["faults_raised"]

    # accessed and dirty, read back by the guest from its own PTEs
    yield "A/D read-faulted page", g["ad_a"] == 0x20, "%#x" % g["ad_a"]
    yield "A/D write-faulted page", g["ad_b"] == 0x60, "%#x" % g["ad_b"]
    yield "A/D read-only-faulted page", g["ad_c"] == 0x60, "%#x" % g["ad_c"]

    # self-modifying code, attributed to the storing vCPU
    yield "pre-patch value", g["smc_before"] == 111, "smc_before=%d" % g["smc_before"]
    yield "post-patch value", g["smc_after"] == 222, "smc_after=%d" % g["smc_after"]
    yield "flush on the storing vCPU", v[0]["smc_flushes"] >= 1, \
        "vCPU0 smc_flushes=%d" % v[0]["smc_flushes"]

    # indirect control transfer
    yield "indirect call reached target", g["icall_marker"] == 0x1234, "%#x" % g["icall_marker"]

    # compare-and-exchange, two-sided
    yield "cmpxchg succeeded", g["cas_ok"] == 1, "cas_ok=%d" % g["cas_ok"]
    yield "cmpxchg failed loading old", g["cas_fail_rax"] == 1, "cas_fail_rax=%d" % g["cas_fail_rax"]

    # interrupts: identity recomputed here from the raw fields
    identity = h["timer_posted"] - g["timer_ticks"]
    yield "post-on-EOI identity", identity == h["timer_pending_at_end"], \
        "posted-ticks=%d pending=%d" % (identity, h["timer_pending_at_end"])
    yield "every tick EOI'd", g["timer_ticks"] == g["timer_eois"], \
        "ticks=%d eois=%d" % (g["timer_ticks"], g["timer_eois"])
    yield "enough ticks to test", g["timer_ticks"] >= 5, "ticks=%d" % g["timer_ticks"]
    yield "IF gated the cli region", g["cli_delta"] == 0, "cli_delta=%d" % g["cli_delta"]
    yield "guest woke from HLT", g["hlt_wakeups"] >= 1, "hlt_wakeups=%d" % g["hlt_wakeups"]
    yield "host recorded a HLT wake", sum(c["hlt_wakes"] for c in v) >= 1, ""
    yield "IPIs delivered one for one", h["ipi_sent"] == g["ipi_recv"], \
        "sent=%d recv=%d" % (h["ipi_sent"], g["ipi_recv"])
    yield "no IPI dropped", h["ipi_dropped"] == 0, "dropped=%d" % h["ipi_dropped"]
    yield "no IPI ack timeout", g["ipi_ack_timeouts"] == 0, "timeouts=%d" % g["ipi_ack_timeouts"]
    yield "at least one IPI", h["ipi_sent"] >= 1, "sent=%d" % h["ipi_sent"]

    # concurrency: overlap and the parallelism floor, both recomputed here
    begins = [c["mark_begin_ns"] for c in v]
    ends = [c["mark_end_ns"] for c in v]
    yield "both windows marked", all(begins) and all(ends), "%r %r" % (begins, ends)
    if all(begins) and all(ends):
        overlap = min(ends) - max(begins)
        span = max(ends) - min(begins)
        ratio = sum(c.get("cpu_ns", 0) for c in v) / span if span else 0.0
        yield "windows overlap", overlap > 0, "overlap=%d ns" % overlap
        yield "parallelism floor", ratio >= PARALLEL_FLOOR, \
            "ratio=%.2f floor=%.2f" % (ratio, PARALLEL_FLOOR)


def evaluate(path):
    d = json.load(open(path))
    fails = [(n, why) for n, ok, why in assertions(d) if not ok]
    total = sum(1 for _ in assertions(d))
    if fails:
        print("%s: FAIL (%d of %d assertions)" % (path, len(fails), total))
        for n, why in fails:
            print("    - %s: %s" % (n, why))
    else:
        print("%s: PASS (%d assertions)" % (path, total))
    return not fails


def selftest():
    """The smallest check that fails if this checker's logic breaks."""
    good = {
        "jit_mapping": "MAP_JIT", "injection": "none", "nvcpu": 2,
        "timer_period_ns": 1000000, "abort": None, "timed_out": False,
        "host": {"timer_posted": 10, "timer_pending_at_end": 0, "ipi_sent": 8, "ipi_dropped": 0},
        "vcpu": [dict(id=0, translations=5, blocks=500, tlb_fills=5, smc_flushes=1,
                      faults_raised=5, hlt_wakes=1, halted_clean=True, finished=True,
                      nested_fault=False, mark_begin_ns=100, mark_end_ns=300, cpu_ns=195),
                 dict(id=1, translations=5, blocks=500, tlb_fills=5, smc_flushes=0,
                      faults_raised=0, hlt_wakes=0, halted_clean=True, finished=True,
                      nested_fault=False, mark_begin_ns=120, mark_end_ns=320, cpu_ns=195)],
        "guest": {"loop_k": 100000, "loop_n": 4096, "counter": 200000,
                  "mp_violations": 0, "mp_timeouts": 0, "iters0": 4096, "iters1": 4096,
                  "pf_count": 4, "ud_count": 1, "gp_count": 0,
                  "timer_ticks": 10, "timer_eois": 10, "ipi_recv": 8, "ipi_ack_timeouts": 0,
                  "hlt_wakeups": 1, "cli_delta": 0, "smc_before": 111, "smc_after": 222,
                  "cas_ok": 1, "cas_fail_rax": 1, "test_a_val": 0xA5A5A5A5,
                  "test_b_mem": 1, "test_c_mem": 1, "vcpu0_done": 1, "vcpu1_done": 1,
                  "push_rsp_after": 0x40003FF8, "push_val": 0x5A5A5A5A,
                  "icall_marker": 0x1234, "ad_a": 0x20, "ad_b": 0x60, "ad_c": 0x60,
                  "fault_log": [{"cr2": WANT_CR2[i], "err": WANT_ERR[i],
                                 "pushed_rip": 100 + i, "expected_rip": 100 + i}
                                for i in range(4)]},
    }
    bad = [n for n, ok, _ in assertions(good) if not ok]
    assert not bad, bad

    def broken(f):
        import copy
        d = copy.deepcopy(good)
        f(d)
        return [n for n, ok, _ in assertions(d) if not ok]

    cases = [
        (lambda d: d["guest"].update(counter=199999), "shared counter exact"),
        (lambda d: d["guest"].update(push_rsp_after=0x40003FF0), "restarted push RSP"),
        (lambda d: d["guest"].update(icall_marker=0), "indirect call reached target"),
        (lambda d: d["guest"].update(ad_b=0x20), "A/D write-faulted page"),
        (lambda d: d["guest"]["fault_log"][3].update(cr2=0), "fault3 CR2"),
        (lambda d: d["guest"]["fault_log"][1].update(err=3), "fault1 error code"),
        (lambda d: d["guest"]["fault_log"][0].update(pushed_rip=999), "fault0 pushed RIP"),
        (lambda d: d.update(jit_mapping="MAP_JIT failed; retried without it"), "frozen jit_mapping"),
        (lambda d: d.update(injection="skip-restart"), "frozen injection"),
        (lambda d: d["guest"].update(loop_k=1, counter=2), "frozen loop_k"),
        (lambda d: [c.update(cpu_ns=90) for c in d["vcpu"]], "parallelism floor"),
        (lambda d: d["vcpu"][1].update(mark_begin_ns=400, mark_end_ns=600), "windows overlap"),
        (lambda d: d["vcpu"][0].update(blocks=5), "vCPU0 reused code cache"),
        (lambda d: (d["vcpu"][0].update(smc_flushes=0), d["vcpu"][1].update(smc_flushes=1)),
         "flush on the storing vCPU"),
        (lambda d: d["vcpu"][0].update(faults_raised=0), "faults raised in translated code"),
        (lambda d: d["vcpu"][0].update(nested_fault=True), "vCPU0 no nested fault"),
        (lambda d: d["host"].update(timer_posted=11), "post-on-EOI identity"),
        (lambda d: d["guest"].update(cas_fail_rax=0), "cmpxchg failed loading old"),
        (lambda d: d.update(timed_out=True), "no watchdog timeout"),
        (lambda d: d.update(abort="boom"), "run completed"),
    ]
    for mutate, want in cases:
        got = broken(mutate)
        assert want in got, (want, got)
    print("recheck selftest OK (%d assertions, %d mutations)"
          % (sum(1 for _ in assertions(good)), len(cases)))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        sys.exit(selftest())
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.exit(0 if all([evaluate(p) for p in sys.argv[1:]]) else 1)

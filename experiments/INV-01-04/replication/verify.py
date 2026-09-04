#!/usr/bin/env python3
"""REV-076 independent replication checker for INV-01-04.
Reviewer context `t01_memory_reviewer`; not derived from src/check.py.
Recomputes INV-01-04's own Metrics thresholds from the raw JSON.
Usage: verify.py <replication result.json> <original result.json>
"""
import json, sys

def main(new_p, old_p):
    n = json.load(open(new_p)); o = json.load(open(old_p))
    fail, rep = [], []
    def inv(c, m):
        if not c: fail.append(m)

    inv(n.get("investigation") == "INV-01-04", "wrong investigation id")
    inv(n.get("mode") == "normal", "not a normal-mode artifact")
    tb = n["mach_timebase_info"]; tick = tb["numer"] / tb["denom"]

    # CLM-01-04-12: toggle support and the three MAP_JIT sizes.
    inv(n["pthread_jit_write_protect_supported_np"] == 1, "toggle support not reported")
    al = n["map_jit_allocation"]
    inv(len(al) == 3 and all(a["ok"] for a in al),
        "MAP_JIT allocation failed: %r" % [(a["bytes"], a["errno_str"]) for a in al if not a["ok"]])
    rep.append(("CLM-01-04-12", [a["ok"] for a in al] == [a["ok"] for a in o["map_jit_allocation"]],
                "supported=%d, sizes ok=%r" % (n["pthread_jit_write_protect_supported_np"],
                                               [a["bytes"] for a in al])))

    # CLM-01-04-13: the batched figure must clear the record's precision target,
    # and the per-call cost must be "tens of ns" (record: median < 200 ns).
    b = n["toggle_cost_batched"]
    inv(b["precision_target_met"] is True,
        "precision target missed: shortest batch %r ticks < %r"
        % (b.get("shortest_batch_ticks"), b.get("precision_target_ticks_per_batch")))
    inv(b["shortest_batch_ticks"] >= b["precision_target_ticks_per_batch"],
        "shortest batch below target")
    pair = b["off_on_pair"]["median_ns"]; per_call = pair / 2.0
    inv(pair < tick, "batched per-pair median %.4f ns is at or above the %.4f ns clock quantum; "
        "the division may have been applied in the tick domain" % (pair, tick))
    inv(per_call < 200.0, "per-call median %.2f ns exceeds the record's 200 ns threshold" % per_call)
    rep.append(("CLM-01-04-13", abs(pair - o["toggle_cost_batched"]["off_on_pair"]["median_ns"]) < 0.25 * pair,
                "%.4f ns/pair (~%.2f ns/call); original %.4f ns/pair"
                % (pair, per_call, o["toggle_cost_batched"]["off_on_pair"]["median_ns"])))

    # CLM-01-04-14: generated code executes after icache invalidation.
    st = n["stub"]
    inv(st["executed"] and st["return_value"] == st["declared_value"],
        "stub returned %r, declared %r" % (st.get("return_value"), st.get("declared_value")))
    rep.append(("CLM-01-04-14", st["return_value"] == o["stub"]["return_value"],
                "stub returned %d" % st["return_value"]))

    # CLM-01-04-15: cross-thread execution WITH the witness that the writer held
    # write access. Without the witness the result is vacuous (REV-042 action 8).
    c = n["concurrent_execute_while_other_thread_writable"]
    inv(c["witness_observed"] == c["witness_expected"],
        "witness %r != %r: the writer did not hold write access"
        % (c.get("witness_observed"), c.get("witness_expected")))
    inv(c["writer_held_write_access"] is True, "probe does not attest the writer held write access")
    inv(c["executed"] and c["return_value"] == st["declared_value"],
        "cross-thread execution did not return the declared value")
    rep.append(("CLM-01-04-15", c["executed"] == o["concurrent_execute_while_other_thread_writable"]["executed"],
                "executed=%s, witness held=%s, returned %d"
                % (c["executed"], c["writer_held_write_access"], c["return_value"])))

    # CLM-01-04-16: both W^X faults.
    se = n["self_execute_while_writable"]; w = n["write_without_toggle"]
    inv(se["executed"] is False and se["signal"], "self-execute-while-writable did not fault")
    inv(w["faulted"] and w["signal"], "write without toggling did not fault")
    rep.append(("CLM-01-04-16",
                se["signal"] == o["self_execute_while_writable"]["signal"]
                and w["signal"] == o["write_without_toggle"]["signal"],
                "self-exec faulted sig %d; write-without-toggle faulted sig %d si_code %d, addr_is_region=%s"
                % (se["signal"], w["signal"], w["si_code"], w["si_addr_is_region"])))

    for cid, ok, d in rep:
        print(("REPRODUCED  %s: %s" if ok else "DIFFERS     %s: %s") % (cid, d))
    for f in fail: print("INVARIANT-FAIL %s" % f)
    print("FAIL" if fail else "PASS")
    return 1 if fail else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))

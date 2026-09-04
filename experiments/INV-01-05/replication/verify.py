#!/usr/bin/env python3
"""REV-076 independent replication checker for INV-01-05.
Reviewer context `t01_memory_reviewer`; not derived from src/check.py.
Recomputes INV-01-05's own Metrics thresholds from the raw JSON, and
independently recomputes the two figures REV-075 examines as partials:
the concurrent-versus-single cost ratio (CLM-01-05-10) and the whole-loop
sign consistency the probe does not emit (CLM-01-05-15).
Usage: verify.py <replication result.json> <original result.json>
"""
import json, sys

def main(new_p, old_p):
    n = json.load(open(new_p)); o = json.load(open(old_p))
    fail, rep, note = [], [], []
    def inv(c, m):
        if not c: fail.append(m)

    inv(n.get("investigation") == "INV-01-05", "wrong investigation id")
    inv(n.get("mode") == "normal", "not a normal-mode artifact")

    # CLM-01-05-07: soundness. One fault per protected page, every address named.
    wt = n["write_tracking"]
    inv(wt["faults_recorded"] == wt["pages_written"],
        "%r faults for %r pages written" % (wt["faults_recorded"], wt["pages_written"]))
    inv(wt["fault_addresses_inside_region"] == wt["faults_recorded"],
        "only %r of %r addresses inside the region" %
        (wt["fault_addresses_inside_region"], wt["faults_recorded"]))
    rep.append(("CLM-01-05-07", wt["faults_recorded"] == o["write_tracking"]["faults_recorded"],
                "%d/%d faults, %d/%d addresses in region, first signal %d"
                % (wt["faults_recorded"], wt["pages_written"],
                   wt["fault_addresses_inside_region"], wt["faults_recorded"], wt["first_signal"])))

    # CLM-01-05-08: round trip, record predicts a low-single-digit-us median.
    rt = wt["fault_round_trip"]
    inv(rt["min_ns"] <= rt["median_ns"] <= rt["p99_ns"] <= rt["max_ns"], "round-trip order statistics inconsistent")
    inv(1000.0 <= rt["median_ns"] <= 10000.0,
        "round-trip median %.1f ns is outside the low-single-digit-microsecond prediction" % rt["median_ns"])
    om = o["write_tracking"]["fault_round_trip"]["median_ns"]
    rep.append(("CLM-01-05-08", abs(rt["median_ns"] - om) <= 0.30 * om,
                "median %.1f ns (original %.1f, %+.1f%%), p99 %.1f, max %.1f"
                % (rt["median_ns"], om, (rt["median_ns"] - om) / om * 100, rt["p99_ns"], rt["max_ns"])))

    # CLM-01-05-09: mprotect median below 1 us.
    mc = n["mprotect_cost"]["per_call"]
    inv(mc["median_ns"] < 1000.0, "mprotect median %.1f ns is not below 1 us" % mc["median_ns"])
    omc = o["mprotect_cost"]["per_call"]["median_ns"]
    rep.append(("CLM-01-05-09", abs(mc["median_ns"] - omc) <= 0.30 * omc,
                "median %.1f ns (original %.1f)" % (mc["median_ns"], omc)))

    # CLM-01-05-10: attribution is the invariant; the cost magnitude is not.
    ct = n["concurrent_tracking"]; per = ct["pages_written_per_thread"]
    inv(ct["identity_matches_range"] == 2 * per,
        "%r of %d faults matched a thread identity to its own range" % (ct["identity_matches_range"], 2 * per))
    inv(ct["identity_mismatches_range"] == 0, "%r faults misattributed across threads" % ct["identity_mismatches_range"])
    inv(ct["faults_without_thread_identity"] == 0, "%r faults carry no thread identity" % ct["faults_without_thread_identity"])
    cmed = max(ct["fault_round_trip_thread_a"]["median_ns"], ct["fault_round_trip_thread_b"]["median_ns"])
    delta = (cmed - rt["median_ns"]) / rt["median_ns"] * 100.0
    note.append("CLM-01-05-10 cost effect recomputed independently: concurrent median %.1f ns vs "
                "single-thread %.1f ns = %+.2f%%. The record reports +4.3%% and withdraws the "
                "contention reading; this replication neither confirms nor refutes a contention "
                "mechanism, and the single-thread spread (min %.1f, p99 %.1f) exceeds the effect."
                % (cmed, rt["median_ns"], delta, rt["min_ns"], rt["p99_ns"]))
    rep.append(("CLM-01-05-10", ct["identity_mismatches_range"] == 0,
                "attribution %d/%d, 0 mismatches, 0 without identity; cost effect %+.2f%%"
                % (ct["identity_matches_range"], 2 * per, delta)))

    # CLM-01-05-11: granularity. Sub-page rejected; four slots of the host page.
    gr = n["granularity"]
    inv(gr["sub_page_mprotect_rc"] == -1 and gr["sub_page_mprotect_errno"] == 22,
        "sub-page mprotect rc=%r errno=%r, expected -1/22" % (gr["sub_page_mprotect_rc"], gr["sub_page_mprotect_errno"]))
    inv(gr["host_page_mprotect_rc"] == 0, "host-page-aligned control failed")
    per_hp = gr["host_page"] // gr["guest_page"]
    slots = gr["guest_slot_writable_after_host_page_protect"]
    inv(not any(slots[:per_hp]), "a guest slot inside the protected host page stayed writable: %r" % slots)
    inv(all(slots[per_hp:]), "a guest slot outside the protected host page became unwritable: %r" % slots)
    rep.append(("CLM-01-05-11", slots == o["granularity"]["guest_slot_writable_after_host_page_protect"],
                "EINVAL on sub-page; slots %r" % slots))

    # CLM-01-05-12: signal classes are distinguishable.
    sc = n["signal_classes"]
    a, b = sc["write_to_mapped_read_only"], sc["write_to_unmapped"]
    inv(a["faulted"] and b["faulted"], "a signal-class case did not fault")
    inv(a["signal"] != b["signal"],
        "protection violation and unmapped access produced the same signal %r; a handler cannot "
        "separate them on signal number alone" % a["signal"])
    rep.append(("CLM-01-05-12",
                (a["signal"], b["signal"]) == (o["signal_classes"]["write_to_mapped_read_only"]["signal"],
                                               o["signal_classes"]["write_to_unmapped"]["signal"]),
                "read-only sig %d/code %d; unmapped sig %d/code %d"
                % (a["signal"], a["si_code"], b["signal"], b["si_code"])))

    # CLM-01-05-13: the isolated marginal must be positive, sign-consistent, and
    # clear the criterion. A resolvable negative marginal invalidates the pair.
    sb = n["software_dirty_bitmap"]; iso = sb["isolated_bitmap_update"]
    inv(sb["bitmap_cost_asserted"] is False, "the record may not assert a bitmap cost from the whole loop")
    med, crit = iso["paired_marginal_median_ns_per_op"], iso["resolution_criterion_ns_per_op"]
    inv(not (iso["marginal_resolvable"] and med < 0),
        "isolated marginal %.4f ns/op is negative and resolvable: adding work cannot make a loop faster" % med)
    inv(iso["paired_sign_consistent"] is True, "isolated marginal not sign-consistent across repetitions")
    inv(iso["marginal_resolvable"] is True and abs(med) > crit,
        "isolated marginal %.4f does not clear the %.4f criterion" % (med, crit))
    om2 = o["software_dirty_bitmap"]["isolated_bitmap_update"]["paired_marginal_median_ns_per_op"]
    rep.append(("CLM-01-05-13", abs(med - om2) <= 0.40 * abs(om2),
                "%.4f ns/op (original %.4f), sign-consistent, criterion %.4f" % (med, om2, crit)))

    # CLM-01-05-15: negative whole-loop median, and the sign-consistency conjunct
    # the record predicts but the probe does not emit for this pair.
    wl = sb["whole_loop"]
    inv(wl["interpretable_as_bitmap_cost"] is False,
        "the whole-loop differential may not be presented as a bitmap cost")
    has_sign = "paired_sign_consistent" in wl
    note.append("CLM-01-05-15: whole-loop paired marginal median %.4f ns/store (original %.4f). "
                "The probe emits sign consistency for the isolated pair only, so the record's "
                "sign-consistency conjunct is %s in this replication either."
                % (wl["paired_marginal_median_ns_per_store"],
                   o["software_dirty_bitmap"]["whole_loop"]["paired_marginal_median_ns_per_store"],
                   "observable" if has_sign else "NOT observable"))
    rep.append(("CLM-01-05-15", wl["paired_marginal_median_ns_per_store"] < 0,
                "median %.4f ns/store, negative as predicted; sign consistency %s"
                % (wl["paired_marginal_median_ns_per_store"],
                   "emitted" if has_sign else "not emitted for this pair")))

    for cid, ok, d in rep:
        print(("REPRODUCED  %s: %s" if ok else "DIFFERS     %s: %s") % (cid, d))
    for x in note: print("NOTE %s" % x)
    for f in fail: print("INVARIANT-FAIL %s" % f)
    print("FAIL" if fail else "PASS")
    return 1 if fail else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))

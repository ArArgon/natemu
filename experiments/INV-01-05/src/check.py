#!/usr/bin/env python3
"""INV-01-05 checker. Reads the probe's JSON and prints PASS or FAIL.

Positive control: a write-tracking mechanism must report one fault per
protected page written and must name a faulting address inside the tracked
region. Failing to name the modified page is unsoundness, not a platform
finding. Exit status 0 = PASS, 1 = FAIL.
"""
import json
import sys

def order_ok(st):
    return st["min_ns"] <= st["median_ns"] <= st["p99_ns"] <= st["max_ns"]

def main(path):
    with open(path) as f:
        d = json.load(f)
    fails, notes = [], []

    if d.get("investigation") != "INV-01-05":
        fails.append("wrong investigation id")
    if not d.get("region_mapped"):
        notes.append("region could not be mapped — platform finding")
        return report(fails, notes)
    if d.get("host_page_size", 0) <= 0:
        fails.append("host_page_size not positive")

    wt = d.get("write_tracking", {})
    if wt.get("faults_recorded") != wt.get("pages_written"):
        fails.append("write_tracking: %r faults for %r protected pages written"
                     % (wt.get("faults_recorded"), wt.get("pages_written")))
    if wt.get("fault_addresses_inside_region") != wt.get("faults_recorded"):
        fails.append("write_tracking: only %r of %r fault addresses lie inside "
                     "the tracked region"
                     % (wt.get("fault_addresses_inside_region"),
                        wt.get("faults_recorded")))
    if not wt.get("first_signal"):
        fails.append("write_tracking: no signal number recorded")
    if not order_ok(wt.get("fault_round_trip", {"min_ns": 1, "median_ns": 0,
                                                "p99_ns": 0, "max_ns": 0})):
        fails.append("write_tracking: round-trip order statistics inconsistent")

    mc = d.get("mprotect_cost", {}).get("per_call")
    if not mc or not order_ok(mc):
        fails.append("mprotect_cost: missing or inconsistent")

    ct = d.get("concurrent_tracking")
    if ct is None:
        notes.append("concurrent case skipped (region too small)")
    else:
        per = ct["pages_written_per_thread"]
        if ct.get("faults_recorded") != 2 * per:
            fails.append("concurrent: %r faults for %d protected pages written"
                         % (ct.get("faults_recorded"), 2 * per))
        if ct.get("in_thread_a_range") != per or ct.get("in_thread_b_range") != per:
            fails.append("concurrent: per-thread fault attribution wrong (a=%r b=%r, "
                         "expected %d each)"
                         % (ct.get("in_thread_a_range"), ct.get("in_thread_b_range"), per))

    gr = d.get("granularity", {})
    if gr.get("mapped"):
        if gr.get("sub_page_mprotect_rc") == -1 and not gr.get("sub_page_mprotect_errno"):
            fails.append("granularity: sub-page mprotect failed without an errno")
        if gr.get("host_page_mprotect_rc") != 0:
            fails.append("granularity: host-page-aligned mprotect control failed")
        slots = gr.get("guest_slot_writable_after_host_page_protect", [])
        if len(slots) != 2 * gr["host_page"] // gr["guest_page"]:
            fails.append("granularity: wrong slot count")
        elif any(slots[: gr["host_page"] // gr["guest_page"]]):
            fails.append("granularity: a guest slot inside the protected host page "
                         "stayed writable — the protection control did not take effect")

    sc = d.get("signal_classes", {})
    for k in ("write_to_mapped_read_only", "write_to_unmapped"):
        e = sc.get(k, {})
        if not e.get("faulted"):
            fails.append("signal_classes: %s did not fault" % k)
        elif not e.get("signal"):
            fails.append("signal_classes: %s faulted without a signal number" % k)

    sb = d.get("software_dirty_bitmap", {})
    if sb.get("stores", 0) < 1000:
        fails.append("software_dirty_bitmap: sample too small")
    if not (sb.get("warmup_pass") and sb.get("order_alternated")):
        fails.append("software_dirty_bitmap: warm-up and order alternation not recorded")
    # Adding a bitmap store to every store cannot make the loop faster. An
    # inversion larger than the run-to-run spread means the comparison measured
    # order or warm-up rather than the work; an inversion inside the spread
    # means the microbenchmark cannot resolve the cost, which is a finding.
    d = sb.get("overhead_ns_per_store", 0.0)
    res = sb.get("resolution_ns_per_store", 0.0)
    if "overhead_resolvable" not in sb:
        fails.append("software_dirty_bitmap: resolvability not declared")
    elif not sb["overhead_resolvable"]:
        notes.append("software_dirty_bitmap: differential %.3f ns/store is inside the "
                     "%.3f ns/store run-to-run spread; this microbenchmark does not "
                     "resolve the bitmap cost and the absolute figures stand alone"
                     % (d, res))
    elif d < 0:
        notes.append("software_dirty_bitmap: differential %.3f ns/store is negative "
                     "beyond the %.3f ns/store spread; the loops differ in more than "
                     "the bitmap update and the differential is not a bitmap cost"
                     % (d, res))

    return report(fails, notes)

def report(fails, notes):
    for n in notes:
        print("NOTE %s" % n)
    for f in fails:
        print("FAIL %s" % f)
    print("FAIL" if fails else "PASS")
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))

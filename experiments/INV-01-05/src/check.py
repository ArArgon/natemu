#!/usr/bin/env python3
"""INV-01-05 checker. Reads the probe's JSON and prints PASS or FAIL.

Positive control: a write-tracking mechanism must report one fault per
protected page written and must name a faulting address inside the tracked
region. Failing to name the modified page is unsoundness, not a platform
finding. Exit status 0 = PASS, 1 = FAIL.

Usage: check.py <result.json> <expected-mode>
"""
import json
import sys

def order_ok(st):
    return st["min_ns"] <= st["median_ns"] <= st["p99_ns"] <= st["max_ns"]

def main(path, expected_mode):
    with open(path) as f:
        doc = json.load(f)
    d = doc
    fails, notes = [], []

    if d.get("investigation") != "INV-01-05":
        fails.append("wrong investigation id")
    if d.get("mode") != expected_mode:
        fails.append("mode is %r, expected %r: a negative-mode artifact must "
                     "never be checked as a normal one"
                     % (d.get("mode"), expected_mode))
    tb = d.get("mach_timebase_info", {})
    if not (tb.get("numer") and tb.get("denom")):
        fails.append("mach_timebase_info not emitted")
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
        # The attribution control that can actually fail: the recorded thread
        # identity must match the range the faulting address falls in.
        if ct.get("faults_without_thread_identity"):
            fails.append("concurrent: %r faults carry no thread identity, so "
                         "attribution is unverifiable"
                         % ct["faults_without_thread_identity"])
        if ct.get("identity_mismatches_range"):
            fails.append("concurrent: %r faults were recorded by a thread whose "
                         "range does not contain the faulting address"
                         % ct["identity_mismatches_range"])
        if ct.get("identity_matches_range") != 2 * per:
            fails.append("concurrent: %r of %d faults matched a thread identity "
                         "to its own range"
                         % (ct.get("identity_matches_range"), 2 * per))

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
    if sb.get("stores_per_repetition", 0) < 1000:
        fails.append("software_dirty_bitmap: sample too small")
    if sb.get("warmup_passes_per_variant", 0) < 1:
        fails.append("software_dirty_bitmap: no warm-up pass recorded (value %r)"
                     % sb.get("warmup_passes_per_variant"))
    if sb.get("order_alternated_by_repetition") is not True:
        fails.append("software_dirty_bitmap: order alternation not recorded")
    if sb.get("bitmap_cost_asserted") is not False:
        fails.append("software_dirty_bitmap: this design may not assert a bitmap cost")
    wl = sb.get("whole_loop", {})
    if wl.get("interpretable_as_bitmap_cost") is not False:
        fails.append("software_dirty_bitmap: the whole-loop differential may not be "
                     "presented as a bitmap cost")
    iso = sb.get("isolated_bitmap_update", {})
    med = iso.get("paired_marginal_median_ns_per_op", 0.0)
    crit = iso.get("resolution_criterion_ns_per_op", 0.0)
    if "marginal_resolvable" not in iso:
        fails.append("software_dirty_bitmap: isolated-pair resolvability not declared")
    elif iso["marginal_resolvable"] and med < 0:
        # Adding a bitmap update to a loop cannot make it faster. A resolvable
        # inversion means the two variants differ in more than that update.
        fails.append("software_dirty_bitmap: isolated paired marginal %.4f ns/op is "
                     "negative and resolvable against the %.4f ns/op criterion; the "
                     "comparison is invalid" % (med, crit))
    elif not iso["marginal_resolvable"]:
        notes.append("software_dirty_bitmap: isolated paired marginal %.4f ns/op does "
                     "not clear the %.4f ns/op criterion; no bitmap cost is "
                     "established" % (med, crit))

    return report(fails, notes)

def report(fails, notes):
    for n in notes:
        print("NOTE %s" % n)
    for f in fails:
        print("FAIL %s" % f)
    print("FAIL" if fails else "PASS")
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))

#!/usr/bin/env python3
"""INV-01-03 checker. Reads the probe's JSON and prints PASS or FAIL.

Usage: check.py <result.json> <expected-mode>

It validates structural completeness and the probe's own positive controls.
It does not judge whether the platform behaves any particular way: an absent
capability is a finding, not a checker failure. Exit status 0 = PASS, 1 = FAIL.
"""
import json
import sys

def main(path, expected_mode):
    with open(path) as f:
        d = json.load(f)
    fails, notes = [], []

    if d.get("investigation") != "INV-01-03":
        fails.append("wrong investigation id")
    if d.get("mode") != expected_mode:
        fails.append("mode is %r, expected %r: a negative-mode artifact must "
                     "never be checked as a normal one"
                     % (d.get("mode"), expected_mode))
    tb = d.get("mach_timebase_info", {})
    if not (tb.get("numer") and tb.get("denom")):
        fails.append("mach_timebase_info not emitted")

    # PAGE_SIZE preprocesses to vm_page_size, so it is not compared; the
    # sysctl reading is, when it is available. The number compared must equal
    # the number the probe declares independent.
    ps = d.get("page_size", {})
    vals = [ps.get(k) for k in ("vm_page_size", "getpagesize",
                                "sysconf__SC_PAGESIZE")]
    if ps.get("sysctl_hw_pagesize_ok"):
        vals.append(ps.get("sysctl_hw_pagesize"))
    if ps.get("compile_time_PAGE_SIZE_is_independent") is not False:
        fails.append("page_size: PAGE_SIZE must be declared non-independent")
    if ps.get("independent_reading_count") != len(vals):
        fails.append("page_size: declares %r independent readings, %d compared"
                     % (ps.get("independent_reading_count"), len(vals)))
    if any(not isinstance(v, int) or v <= 0 for v in vals):
        fails.append("page_size: missing or non-positive reading")
    elif len(set(vals)) != 1:
        notes.append("page_size readings disagree: %r" % (vals,))

    res = d.get("reservations")
    if not isinstance(res, list) or len(res) != 3:
        fails.append("reservations: expected 3 entries")
    else:
        for r in res:
            if not r.get("ok") and not r.get("errno"):
                fails.append("reservation %s failed without an errno" % r.get("bytes"))

    ct = d.get("commit_touch", {})
    if ct.get("mapped"):
        want = ct["touch_bytes"] // ct["step_bytes"]
        if ct.get("pages_touched") != want:
            fails.append("commit_touch: pages_touched %r != %r"
                         % (ct.get("pages_touched"), want))
        if not ct.get("task_info_ok"):
            notes.append("commit_touch: task_info unavailable")
        else:
            # The record's declared threshold: resident delta within 10% of the
            # touched amount.
            want_b, got_b = ct["touch_bytes"], ct.get("resident_delta", 0)
            if abs(got_b - want_b) > 0.10 * want_b:
                notes.append("commit_touch: resident delta %r is outside 10%% of the "
                             "%r bytes touched — platform finding" % (got_b, want_b))

    al = d.get("alias", {})
    if al.get("attempted") and al.get("kr_remap") == 0:
        # Positive control: bytes written through a mapping must read back
        # through that same mapping. No platform outcome can excuse a miss.
        if al.get("self_readback_matched") != al.get("bytes_total"):
            fails.append("alias: self-readback positive control failed (%r of %r)"
                         % (al.get("self_readback_matched"), al.get("bytes_total")))
        if not al.get("distinct_addresses"):
            fails.append("alias: remap returned the same address, so no aliasing "
                         "was exercised and the read-through result is vacuous")
        if al.get("bytes_matched") != al.get("bytes_total"):
            notes.append("alias: read-through mismatch (%r of %r) — platform finding"
                         % (al.get("bytes_matched"), al.get("bytes_total")))
        if al.get("bytes_matched_reverse") != al.get("bytes_total"):
            notes.append("alias: reverse read-through mismatch (%r of %r) — "
                         "platform finding"
                         % (al.get("bytes_matched_reverse"), al.get("bytes_total")))

    gr = d.get("granularity", {})
    if gr.get("mapped"):
        if gr.get("mprotect_rc") not in (0, -1):
            fails.append("granularity: bad mprotect_rc")
        if gr.get("mprotect_rc") == -1 and not gr.get("mprotect_errno"):
            fails.append("granularity: mprotect failed without an errno")
        n = len(gr.get("slot_writable", []))
        if n != 2 * gr["host_page"] // gr["guest_page"]:
            fails.append("granularity: wrong slot count %d" % n)

    ad = d.get("advice", {})
    if ad.get("mapped"):
        if "mincore_first_byte_flags" not in ad:
            fails.append("advice: mincore flags not decoded")
        if ad.get("mincore_rc") == 0 and not ad.get("mincore_written_page_modified"):
            notes.append("advice: MINCORE_MODIFIED not set on a freshly written "
                         "page — platform finding relevant to Q-01-05")

    for note in notes:
        print("NOTE %s" % note)
    for f in fails:
        print("FAIL %s" % f)
    print("FAIL" if fails else "PASS")
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))

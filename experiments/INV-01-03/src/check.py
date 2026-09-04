#!/usr/bin/env python3
"""INV-01-03 checker. Reads the probe's JSON and prints PASS or FAIL.

It validates structural completeness and the probe's own positive controls.
It does not judge whether the platform behaves any particular way: an absent
capability is a finding, not a checker failure. Exit status 0 = PASS, 1 = FAIL.
"""
import json
import sys

def main(path):
    with open(path) as f:
        d = json.load(f)
    fails, notes = [], []

    if d.get("investigation") != "INV-01-03":
        fails.append("wrong investigation id")

    ps = d.get("page_size", {})
    vals = [ps.get(k) for k in ("vm_page_size", "getpagesize",
                                "sysconf__SC_PAGESIZE", "compile_time_PAGE_SIZE")]
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

    al = d.get("alias", {})
    if al.get("attempted") and al.get("kr_remap") == 0:
        # Positive control: bytes written through a mapping must read back
        # through that same mapping. No platform outcome can excuse a miss.
        if al.get("self_readback_matched") != al.get("bytes_total"):
            fails.append("alias: self-readback positive control failed (%r of %r)"
                         % (al.get("self_readback_matched"), al.get("bytes_total")))
        if al.get("bytes_matched") != al.get("bytes_total"):
            notes.append("alias: read-through mismatch (%r of %r) — platform finding"
                         % (al.get("bytes_matched"), al.get("bytes_total")))

    gr = d.get("granularity", {})
    if gr.get("mapped"):
        if gr.get("mprotect_rc") not in (0, -1):
            fails.append("granularity: bad mprotect_rc")
        if gr.get("mprotect_rc") == -1 and not gr.get("mprotect_errno"):
            fails.append("granularity: mprotect failed without an errno")
        n = len(gr.get("slot_writable", []))
        if n != 2 * gr["host_page"] // gr["guest_page"]:
            fails.append("granularity: wrong slot count %d" % n)

    for note in notes:
        print("NOTE %s" % note)
    for f in fails:
        print("FAIL %s" % f)
    print("FAIL" if fails else "PASS")
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))

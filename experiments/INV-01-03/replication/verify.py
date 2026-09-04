#!/usr/bin/env python3
"""REV-076 independent replication checker for INV-01-03.

Written for the replication gate by reviewer context `t01_memory_reviewer`.
It is NOT derived from experiments/INV-01-03/src/check.py: it recomputes the
invariants and decision thresholds stated in INV-01-03's own Metrics table and
Hypothesis/falsifier section directly from the raw JSON, and compares them with
the numbers the record reports as Observed.

Usage: verify.py <replication result.json> <original result.json>
Exit 0 if every invariant holds; 1 otherwise. Reproduction of the ORIGINAL
numbers is reported per claim and does not by itself set the exit status.
"""
import json, sys

def load(p):
    with open(p) as f: return json.load(f)

def main(new_p, old_p):
    n, o = load(new_p), load(old_p)
    fail, rep = [], []

    def inv(cond, msg):
        if not cond: fail.append(msg)

    inv(n.get("investigation") == "INV-01-03", "wrong investigation id")
    inv(n.get("mode") == "normal", "not a normal-mode artifact")

    # CLM-01-03-11: the four independent readings agree; PAGE_SIZE is not one.
    ps = n["page_size"]
    reads = [ps["vm_page_size"], ps["getpagesize"], ps["sysconf__SC_PAGESIZE"]]
    if ps["sysctl_hw_pagesize_ok"]: reads.append(ps["sysctl_hw_pagesize"])
    inv(ps["compile_time_PAGE_SIZE_is_independent"] is False,
        "PAGE_SIZE must be declared non-independent")
    inv(ps["independent_reading_count"] == len(reads),
        "independent_reading_count %r != %d compared" % (ps["independent_reading_count"], len(reads)))
    inv(len(set(reads)) == 1, "page-size readings disagree: %r" % reads)
    rep.append(("CLM-01-03-11", reads[0] == o["page_size"]["vm_page_size"],
                "page size %d (original %d)" % (reads[0], o["page_size"]["vm_page_size"])))

    # CLM-01-03-07: 8 GiB / 64 GiB / 1 TiB PROT_NONE reservations.
    res = n["reservations"]
    inv(len(res) == 3, "expected 3 reservations")
    inv(all(r["ok"] for r in res),
        "reservation failed: %r" % [(r["bytes"], r["errno_str"]) for r in res if not r["ok"]])
    rep.append(("CLM-01-03-07", [r["ok"] for r in res] == [r["ok"] for r in o["reservations"]],
                "reservations ok=%r" % [r["ok"] for r in res]))

    # CLM-01-03-08: resident delta within the record's declared 10% of touched.
    ct = n["commit_touch"]
    inv(ct["mapped"], "8 GiB mapping failed")
    inv(ct["pages_touched"] == ct["touch_bytes"] // ct["step_bytes"],
        "pages_touched %r != %r" % (ct["pages_touched"], ct["touch_bytes"] // ct["step_bytes"]))
    dev = abs(ct["resident_delta"] - ct["touch_bytes"]) / ct["touch_bytes"]
    inv(dev <= 0.10, "resident delta %r deviates %.2f%% from the %r touched (>10%%)"
        % (ct["resident_delta"], dev * 100, ct["touch_bytes"]))
    phys = ct["phys_footprint_after"] - ct["phys_footprint_before"]
    rep.append(("CLM-01-03-08", ct["resident_delta"] == o["commit_touch"]["resident_delta"],
                "resident delta %d (%.4f%% deviation), phys_footprint delta %d"
                % (ct["resident_delta"], dev * 100, phys)))

    # CLM-01-03-09: aliasing, both directions, with the distinctness control.
    al = n["alias"]
    inv(al["kr_remap"] == 0, "mach_vm_remap failed: kr=%r" % al["kr_remap"])
    inv(al["distinct_addresses"], "remap returned the same address; aliasing not exercised")
    for k in ("self_readback_matched", "bytes_matched", "bytes_matched_reverse"):
        inv(al[k] == al["bytes_total"], "alias %s %r of %r" % (k, al[k], al["bytes_total"]))
    rep.append(("CLM-01-03-09", al["bytes_matched"] == o["alias"]["bytes_matched"],
                "alias %d/%d forward, %d/%d reverse"
                % (al["bytes_matched"], al["bytes_total"],
                   al["bytes_matched_reverse"], al["bytes_total"])))

    # CLM-01-03-10: sub-host-page mprotect rejected, not silently rounded.
    gr = n["granularity"]
    inv(gr["mprotect_rc"] == -1, "sub-host-page mprotect was NOT rejected (rc=%r): "
        "silent rounding is the record's most consequential falsifier" % gr["mprotect_rc"])
    inv(gr["mprotect_errno"] == 22, "sub-page mprotect errno %r, expected 22 (EINVAL)" % gr["mprotect_errno"])
    inv(len(gr["slot_writable"]) == 2 * gr["host_page"] // gr["guest_page"], "wrong slot count")
    rep.append(("CLM-01-03-10", gr["mprotect_errno"] == o["granularity"]["mprotect_errno"],
                "rc=%d errno=%d slots=%r" % (gr["mprotect_rc"], gr["mprotect_errno"], gr["slot_writable"])))

    # CLM-01-03-14: MINCORE_MODIFIED set on a written page, cleared by madvise.
    ad = n["advice"]
    inv(ad["mincore_rc"] == 0, "mincore failed")
    inv(ad["mincore_written_page_modified"] is True,
        "MINCORE_MODIFIED not set on a freshly written page")
    MOD = 0x4
    cleared = not (ad["mincore_first_byte_after_madvise"] & MOD)
    inv(cleared, "MINCORE_MODIFIED still set after MADV_FREE_REUSABLE (byte %d)"
        % ad["mincore_first_byte_after_madvise"])
    rep.append(("CLM-01-03-14",
                ad["mincore_written_page_byte"] == o["advice"]["mincore_written_page_byte"]
                and ad["mincore_first_byte_after_madvise"] == o["advice"]["mincore_first_byte_after_madvise"],
                "written byte %d (MODIFIED set), after madvise %d (MODIFIED clear)"
                % (ad["mincore_written_page_byte"], ad["mincore_first_byte_after_madvise"])))

    for cid, ok, detail in rep:
        print("REPRODUCED  %s: %s" % (cid, detail) if ok else
              "DIFFERS     %s: %s" % (cid, detail))
    for f in fail: print("INVARIANT-FAIL %s" % f)
    print("FAIL" if fail else "PASS")
    return 1 if fail else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))

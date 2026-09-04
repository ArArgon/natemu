#!/usr/bin/env python3
"""INV-01-04 checker. Reads the probe's JSON and prints PASS or FAIL.

It validates structural completeness and the probe's positive controls. A
platform capability that is absent is a finding, not a checker failure; a stub
that returns something other than the value the probe declared it assembled is
a defect. Exit status 0 = PASS, 1 = FAIL.
"""
import json
import sys

def main(path):
    with open(path) as f:
        d = json.load(f)
    fails, notes = [], []

    if d.get("investigation") != "INV-01-04":
        fails.append("wrong investigation id")

    if d.get("pthread_jit_write_protect_supported_np") not in (0, 1):
        fails.append("pthread_jit_write_protect_supported_np: not 0 or 1")

    alloc = d.get("map_jit_allocation")
    if not isinstance(alloc, list) or len(alloc) != 3:
        fails.append("map_jit_allocation: expected 3 entries")
    else:
        for a in alloc:
            if not a.get("ok"):
                if not a.get("errno"):
                    fails.append("MAP_JIT %s failed without an errno" % a.get("bytes"))
                else:
                    notes.append("MAP_JIT %s unavailable (errno %d) — platform finding"
                                 % (a.get("bytes"), a["errno"]))

    if not d.get("jit_region_mapped"):
        notes.append("JIT region could not be mapped — platform finding")
        return report(fails, notes)

    for key in ("toggle_cost", "toggle_cost_batched"):
        blk = d.get(key)
        if not isinstance(blk, dict):
            fails.append("%s: missing" % key)
            continue
        for name, st in blk.items():
            if not isinstance(st, dict):
                continue
            if st.get("n", 0) < 1000:
                fails.append("%s/%s: sample count too small" % (key, name))
            if not (st["min_ns"] <= st["median_ns"] <= st["p99_ns"] <= st["max_ns"]):
                fails.append("%s/%s: order statistics inconsistent" % (key, name))

    st = d.get("stub", {})
    # Positive control: the stub was assembled to return declared_value.
    if st.get("executed"):
        if st.get("return_value") != st.get("declared_value"):
            fails.append("stub: returned %r, probe declared %r"
                         % (st.get("return_value"), st.get("declared_value")))
    else:
        fails.append("stub: did not execute after a successful MAP_JIT mapping")

    c = d.get("concurrent_execute_while_other_thread_writable", {})
    if c.get("executed"):
        if c.get("return_value") != st.get("declared_value"):
            fails.append("concurrent: returned %r, probe declared %r"
                         % (c.get("return_value"), st.get("declared_value")))
    elif not c.get("signal"):
        fails.append("concurrent: neither executed nor recorded a signal")
    else:
        notes.append("concurrent execution faulted with signal %d — platform finding"
                     % c["signal"])

    w = d.get("write_without_toggle", {})
    if w.get("faulted") and not w.get("signal"):
        fails.append("write_without_toggle: faulted without a signal number")
    if not w.get("faulted"):
        notes.append("write without toggling did not fault — platform finding")

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

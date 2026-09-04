#!/usr/bin/env python3
"""INV-01-06 checker. Reads the probe's JSON and prints PASS or FAIL.

Positive control: every fault case must be delivered, must report the faulting
PC inside the stub that faulted, and must resume with the exact value the probe
declared it wrote into the resumed register. Returning control without the
requested state is not a restart, so a mismatch is a FAIL rather than a
platform finding. Exit status 0 = PASS, 1 = FAIL.
"""
import json
import sys

MEMORY_CASES = ("unmapped_load_direct_pc", "unmapped_load_set_pc_fptr")

def order_ok(st):
    return st["min_ns"] <= st["median_ns"] <= st["p99_ns"] <= st["max_ns"]

def main(path):
    with open(path) as f:
        d = json.load(f)
    fails, notes = [], []

    if d.get("investigation") != "INV-01-06":
        fails.append("wrong investigation id")
    if d.get("end") is not True:
        fails.append("record truncated: probe did not reach the end")

    ss = d.get("state_structs", {})
    for k in ("__DARWIN_OPAQUE_ARM_THREAD_STATE64", "has_feature_ptrauth_calls",
              "sizeof_esr_field", "sizeof_far_field", "exception_state_variant"):
        if k not in ss:
            fails.append("state_structs: %s missing" % k)

    if not d.get("stubs_built"):
        notes.append("JIT stubs could not be built — platform finding")
        return report(fails, notes)

    cases = d.get("fault_and_resume", {})
    if len(cases) < 4:
        fails.append("fault_and_resume: expected 4 cases, got %d" % len(cases))
    for name, c in cases.items():
        if not c.get("fault_seen"):
            fails.append("%s: no fault delivered" % name)
            continue
        if not c.get("signal"):
            fails.append("%s: fault delivered without a signal number" % name)
        if not c.get("pc_at_fault_is_stub"):
            fails.append("%s: reported PC is not the faulting stub" % name)
        if not c.get("sp_nonzero"):
            fails.append("%s: stack pointer not reported" % name)
        # The restart control.
        if c.get("observed_return_value") != c.get("declared_resume_value"):
            fails.append("%s: resumed with %r, probe declared %r"
                         % (name, c.get("observed_return_value"),
                            c.get("declared_resume_value")))
        if name in MEMORY_CASES and not c.get("far_matches_access"):
            fails.append("%s: FAR does not match the attempted access" % name)
        if name not in MEMORY_CASES and c.get("far") not in ("0x0", None):
            notes.append("%s: FAR is %s on a non-memory fault" % (name, c.get("far")))

    lat = d.get("delivery_latency", {}).get("signal_delivery")
    if not lat or lat.get("n", 0) < 1000:
        fails.append("delivery_latency: missing or sample too small")
    elif not order_ok(lat):
        fails.append("delivery_latency: order statistics inconsistent")

    cc = d.get("concurrent_faults", {})
    for t in ("thread_a", "thread_b"):
        e = cc.get(t, {})
        if not e.get("correct"):
            fails.append("concurrent_faults: %s saw FAR %s, expected %s"
                         % (t, e.get("observed_far"), e.get("expected_far")))
        if e.get("return_value") != cc.get("declared_resume_value"):
            fails.append("concurrent_faults: %s resumed with %r, probe declared %r"
                         % (t, e.get("return_value"), cc.get("declared_resume_value")))

    mep = d.get("mach_exception_ports", {})
    for k in ("kr_port_allocate", "kr_insert_send_right",
              "kr_task_get_exception_ports", "kr_thread_set_exception_ports",
              "kr_thread_restore_exception_ports"):
        if k not in mep:
            fails.append("mach_exception_ports: %s missing" % k)
        elif mep[k] != 0:
            notes.append("mach_exception_ports: %s returned %d — platform finding"
                         % (k, mep[k]))
    if mep.get("exception_delivered_through_port"):
        fails.append("mach_exception_ports: probe claims a delivery it does not perform")

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

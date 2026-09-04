#!/usr/bin/env python3
"""REV-076 independent replication checker for INV-01-06.
Reviewer context `t01_memory_reviewer`; not derived from src/check.py.
Recomputes INV-01-06's own Metrics thresholds from the raw JSON.
Usage: verify.py <replication result.json> <original result.json>
"""
import json, sys

EXPECT_SIG = {"unmapped_load": 11, "undefined_instruction": 4, "breakpoint_brk": 5}

def main(new_p, old_p):
    n = json.load(open(new_p)); o = json.load(open(old_p))
    fail, rep, note = [], [], []
    def inv(c, m):
        if not c: fail.append(m)

    inv(n.get("investigation") == "INV-01-06", "wrong investigation id")
    inv(n.get("mode") == "normal", "not a normal-mode artifact")
    inv(n.get("end") is True, "record truncated")

    # CLM-01-06-06: compile configuration, plus the uc_mcsize observation that
    # (unlike the __esr width) does bear on what the kernel delivered.
    ss = n["state_structs"]
    inv(ss["__DARWIN_OPAQUE_ARM_THREAD_STATE64"] == 0, "opaque layout selected; probe assumes non-opaque")
    inv(ss["has_feature_ptrauth_calls"] == 0, "ptrauth enabled; this is the owed cell, not this one")
    rep.append(("CLM-01-06-06",
                (ss["__DARWIN_OPAQUE_ARM_THREAD_STATE64"], ss["has_feature_ptrauth_calls"], ss["sizeof_esr_field"])
                == (o["state_structs"]["__DARWIN_OPAQUE_ARM_THREAD_STATE64"],
                    o["state_structs"]["has_feature_ptrauth_calls"], o["state_structs"]["sizeof_esr_field"]),
                "opaque=%d ptrauth=%d esr=%dB mcontext=%dB"
                % (ss["__DARWIN_OPAQUE_ARM_THREAD_STATE64"], ss["has_feature_ptrauth_calls"],
                   ss["sizeof_esr_field"], ss["sizeof_mcontext64"])))

    # CLM-01-06-08: one accessor-based resume path, and the build fact that makes
    # it indistinguishable from a direct assignment.
    rp = n["resume_path"]
    inv("accessor_and_direct_assignment_identical_in_this_build" in rp,
        "resume-path configuration not recorded")
    inv(rp["used"].endswith("set_pc_fptr"), "resume path is not the SDK accessor")

    # CLM-01-06-07/-09: three fault classes, each delivered with full state and
    # each individually restartable.
    cases = n["fault_and_resume"]
    inv(set(cases) == set(EXPECT_SIG), "fault cases %r, expected %r" % (sorted(cases), sorted(EXPECT_SIG)))
    for name, c in cases.items():
        inv(c["fault_seen"], "%s: no fault delivered" % name)
        inv(c["pc_at_fault_is_stub"], "%s: reported PC is not the faulting stub" % name)
        inv(c["sp_nonzero"], "%s: stack pointer not reported" % name)
        inv(c["handler_installed_state"], "%s: handler did not attest it installed the resume state" % name)
        inv(c["observed_return_value"] == c["declared_resume_value"],
            "%s: resumed with %r, declared %r" % (name, c["observed_return_value"], c["declared_resume_value"]))
        inv(c["uc_mcsize"] == ss["sizeof_mcontext64"],
            "%s: delivered uc_mcsize %r != compiled sizeof_mcontext64 %r"
            % (name, c["uc_mcsize"], ss["sizeof_mcontext64"]))
        if name in EXPECT_SIG:
            inv(c["signal"] == EXPECT_SIG[name],
                "%s: signal %r, expected %r" % (name, c["signal"], EXPECT_SIG[name]))
    inv(cases["unmapped_load"]["far_matches_access"],
        "unmapped_load: FAR does not match the attempted access")
    sigs = {k: v["signal"] for k, v in cases.items()}
    inv(len(set(sigs.values())) == 3, "the three fault classes did not produce distinct signals: %r" % sigs)
    rep.append(("CLM-01-06-07/-09", sigs == {k: v["signal"] for k, v in o["fault_and_resume"].items()},
                "3 classes delivered, distinct signals %r, all resumed to the sentinel, FAR matched" % sigs))

    # CLM-01-06-10: delivery latency, record predicts a median under 10 us, and
    # no fault may be missed.
    dl = n["delivery_latency"]; lat = dl["signal_delivery"]
    inv(dl["missed_faults"] == 0, "%r attempted faults were never delivered" % dl["missed_faults"])
    inv(lat["n"] >= 1000, "latency sample too small")
    inv(lat["min_ns"] <= lat["median_ns"] <= lat["p99_ns"] <= lat["max_ns"], "latency order statistics inconsistent")
    inv(lat["median_ns"] < 10000.0, "latency median %.1f ns exceeds the 10 us prediction" % lat["median_ns"])
    ol = o["delivery_latency"]["signal_delivery"]["median_ns"]
    rep.append(("CLM-01-06-10", abs(lat["median_ns"] - ol) <= 0.30 * ol,
                "median %.1f ns over %d faults, 0 missed (original %.1f), p99 %.1f, max %.1f"
                % (lat["median_ns"], lat["n"], ol, lat["p99_ns"], lat["max_ns"])))
    cold = [c["cold_delivery_ns_single_observation"] for c in cases.values()]
    note.append("cold single observations %s span %.1fx the steady-state median; the record names "
                "them non-quotable and this replication agrees."
                % (["%.1f" % c for c in cold], max(cold) / lat["median_ns"]))

    # CLM-01-06-11: per-thread state under two concurrent faults.
    cc = n["concurrent_faults"]
    for t in ("thread_a", "thread_b"):
        e = cc[t]
        inv(e["correct"], "%s saw FAR %s, expected %s" % (t, e["observed_far"], e["expected_far"]))
        inv(e["return_value"] == cc["declared_resume_value"],
            "%s resumed with %r, declared %r" % (t, e["return_value"], cc["declared_resume_value"]))
    inv(cc["thread_a"]["observed_far"] != cc["thread_b"]["observed_far"],
        "both threads observed the same FAR; per-thread state is not established")
    rep.append(("CLM-01-06-11", cc["thread_a"]["correct"] and cc["thread_b"]["correct"],
                "both threads saw their own FAR (%s, %s) and resumed to the sentinel"
                % (cc["thread_a"]["observed_far"], cc["thread_b"]["observed_far"])))

    # CLM-01-06-12: Mach exception-port API availability only.
    mp = n["mach_exception_ports"]
    for k in ("kr_port_allocate", "kr_insert_send_right", "kr_task_get_exception_ports",
              "kr_thread_set_exception_ports", "kr_thread_restore_exception_ports"):
        inv(mp[k] == 0, "%s returned %r" % (k, mp[k]))
    inv(mp["exception_delivered_through_port"] is False,
        "the probe claims a Mach delivery it does not perform")
    rep.append(("CLM-01-06-12", mp["task_bad_access_handlers_installed"]
                == o["mach_exception_ports"]["task_bad_access_handlers_installed"],
                "all calls KERN_SUCCESS; %d task-level EXC_BAD_ACCESS handler(s) already installed; "
                "no exception delivered through the port"
                % mp["task_bad_access_handlers_installed"]))

    for cid, ok, d in rep:
        print(("REPRODUCED  %s: %s" if ok else "DIFFERS     %s: %s") % (cid, d))
    for x in note: print("NOTE %s" % x)
    for f in fail: print("INVARIANT-FAIL %s" % f)
    print("FAIL" if fail else "PASS")
    return 1 if fail else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))

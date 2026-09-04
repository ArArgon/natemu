#!/usr/bin/env python3
"""REV-095 independent checker for INV-01-01. Reviewer-written; deliberately
NOT derived from the author's check.py. It recomputes, from the raw JSON
alone, every invariant the Observed claims rest on, and prints one line per
claim so a later reader can trace claim -> field -> value.

Usage: verify.py <unentitled.json> <entitled.json> <negative.json>
Exit 0 if the claimed result reproduced, 1 otherwise.
"""
import json, sys

def main(a):
    if len(a) != 4:
        print("usage: verify.py <unentitled> <entitled> <negative>"); return 2
    u, e, n = (json.load(open(p)) for p in a[1:])
    bad = []
    def want(cond, label, got):
        print(f"{'ok  ' if cond else 'FAIL'} {label}: {got}")
        if not cond: bad.append(label)

    # CLM-01-01-04 (KA-01): MAP_JIT + toggle + distinct-constant callback
    want(u["map_jit"]["mmap_ok"] is True, "CLM-01-01-04 map_jit.mmap_ok unentitled", u["map_jit"]["mmap_ok"])
    j = u["jit_write_protect"]
    want(j["toggle_ok"] is True and j["executed_value"] == j["expected_value"] == 42
         and j["crashed"] is False and j["write_after_reprotect_ok"] is True,
         "CLM-01-01-04 JIT toggle round-trip -> 42", (j["executed_value"], j["crashed"]))
    want(j["write_with_callback_np_ok"] is True
         and j["write_with_callback_np_value"] == j["write_with_callback_np_expected_value"] == 43,
         "CLM-01-01-04 callback path -> 43 (distinct constant)", j["write_with_callback_np_value"])

    # CLM-01-01-10 (F3 input): entitlement gating, both directions
    want(u["hypervisor"]["hv_vm_create_hr"] == "0xfae94007",
         "CLM-01-01-10 unentitled hv_vm_create_hr == HV_DENIED", u["hypervisor"]["hv_vm_create_hr"])
    h = e["hypervisor"]
    want(h["hv_vm_create_hr"] == "0x00000000" and h["hv_vm_create_ok"] is True,
         "CLM-01-01-10 entitled hv_vm_create_hr == HV_SUCCESS", h["hv_vm_create_hr"])
    want(h["hv_vm_get_max_vcpu_count_ok"] is True and h["hv_vcpu_create_ok"] is True,
         "CLM-01-01-10 entitled max_vcpu_count + vcpu_create ok",
         (h["hv_vm_get_max_vcpu_count_value"], h["hv_vcpu_create_ok"]))
    # positive control, recomputed here rather than trusted from run.sh
    want(u["hypervisor"]["hv_vm_create_hr"] != h["hv_vm_create_hr"],
         "positive control: unentitled != entitled", (u["hypervisor"]["hv_vm_create_hr"], h["hv_vm_create_hr"]))
    # unattempted calls must be null, not a plausible error code (REV-039 C5)
    want(all(u["hypervisor"][k] is None for k in
             ("hv_vm_get_max_vcpu_count_hr", "hv_vcpu_create_hr",
              "hv_vcpu_get_sys_reg_id_aa64mmfr0_el1_hr")),
         "unentitled unattempted calls report null", "null")

    # CLM-01-01-11: raw register read only, no decoded fields anywhere
    want(h["hv_vcpu_get_sys_reg_id_aa64mmfr0_el1_ok"] is True
         and isinstance(h["id_aa64mmfr0_el1_raw"], str),
         "CLM-01-01-11 ID_AA64MMFR0_EL1 read (raw)", h["id_aa64mmfr0_el1_raw"])
    want(not any("parange" in k or "tgran" in k for k in h),
         "CLM-01-01-11 no decoded PARange/TGran fields emitted", sorted(h)[:1] and "none")

    # CLM-01-01-13 aliasing; -14 auxiliaries; -19 affinity; -20 clock
    r = u["mach_vm_remap"]
    want(r["ok"] is True and r["alias_write_visible"] is True and r["crashed"] is False,
         "CLM-01-01-13 mach_vm_remap aliasing visible", r["alias_write_visible"])
    want(u["getentropy"]["ok"] and not u["getentropy"]["all_zero_suspicious"]
         and u["posix_openpt"]["ok"] and u["setrlimit_nofile"]["set_ok"]
         and u["qos_affinity"]["pthread_set_qos_class_self_np_rv"] == 0,
         "CLM-01-01-14 auxiliary facilities succeed", "entropy/pty/rlimit/qos ok")
    kr = {d["qos_affinity"]["thread_policy_set_affinity_kr"] for d in (u, e, n)}
    want(kr == {46}, "CLM-01-01-19 THREAD_AFFINITY_POLICY kr stable across all three runs", kr)
    want(all(d["mach_timebase"]["two_sample_smoke_check_nondecreasing"] is True
             and d["mach_timebase"]["numer"] > 0 and d["mach_timebase"]["denom"] > 0
             for d in (u, e)), "CLM-01-01-20 clock smoke check", (u["mach_timebase"]["numer"], u["mach_timebase"]["denom"]))

    # host identity must be self-describing in the artifact
    want(u["host"]["hw_model"] == e["host"]["hw_model"] == "Mac16,10"
         and u["host"]["kern_hv_support"] == 1,
         "host identity + kern.hv_support recorded in artifact",
         (u["host"]["hw_model"], u["host"]["kern_hv_support"]))

    # negative control must be detectable by an independent reader too
    want(n["negative_control"] is True
         and n["jit_write_protect"]["executed_value"] != n["jit_write_protect"]["expected_value"],
         "negative control mis-reports executed_value", n["jit_write_protect"]["executed_value"])

    print("\nREPRODUCED" if not bad else f"\nNOT REPRODUCED ({len(bad)} check(s) failed)")
    return 0 if not bad else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
"""Validate one INV-01-01 probe JSON output against the invariants that a
truthful run must satisfy. Standard library only.

Usage: check.py <path-to-probe-output.json>
Exit 0 and print PASS if every rule holds; exit 1 and print each failing
rule otherwise. This is the code-gate validation tool: it must FAIL on the
probe's own `--negative` output, on a run in which MAP_JIT was unavailable
(KA-01 falsified), and on a run whose Hypervisor entitlement outcome does
not match its own label (REV-039 C1 / REV-038 M6c). Internal-consistency
rules alone are not enough: a run in which every capability simply failed
must not pass.
"""
import json
import sys

HV_SUCCESS = "0x00000000"
HV_DENIED = "0xfae94007"


def check(d):
    failures = []

    def require(cond, msg):
        if not cond:
            failures.append(msg)

    label = d.get("label", "")
    negative = bool(d.get("negative_control"))

    host = d.get("host", {})
    require(host.get("hw_pagesize", 0) > 0, "host.hw_pagesize must be > 0")
    require(host.get("hw_ncpu", 0) >= 1, "host.hw_ncpu must be >= 1")
    require(bool(host.get("hw_model")), "host.hw_model must be non-empty")

    mj = d.get("map_jit", {})
    if mj.get("mmap_ok"):
        require(mj.get("size", 0) > 0, "map_jit.size must be > 0 when mmap_ok")

    jwp = d.get("jit_write_protect", {})
    # The probe can only ever have actually executed the embedded constant
    # (42) when it reports toggle_ok; a different value under toggle_ok
    # means the field was fabricated, not measured. This is the rule the
    # --negative mode is designed to trip.
    if jwp.get("toggle_ok"):
        require(
            jwp.get("executed_value") == jwp.get("expected_value", 42),
            "jit_write_protect.executed_value must equal expected_value "
            "when toggle_ok is true",
        )
    if jwp.get("write_with_callback_np_ok"):
        require(
            jwp.get("write_with_callback_np_value")
            == jwp.get("write_with_callback_np_expected_value", 43),
            "jit_write_protect.write_with_callback_np_value must equal "
            "write_with_callback_np_expected_value when "
            "write_with_callback_np_ok is true",
        )
    # A section that did not crash must not report a nonzero crash_signal
    # (REV-039 C1's "self-contradiction the checker has no rule against").
    require(
        not (jwp.get("crashed") is False and jwp.get("crash_signal", 0) != 0),
        "jit_write_protect.crash_signal must be 0 when crashed is false",
    )

    remap = d.get("mach_vm_remap", {})
    if remap.get("ok"):
        require(
            remap.get("alias_write_visible") is True,
            "mach_vm_remap.alias_write_visible must be true when ok is true",
        )
    require(
        not (remap.get("crashed") is False and remap.get("crash_signal", 0) != 0),
        "mach_vm_remap.crash_signal must be 0 when crashed is false",
    )

    tb = d.get("mach_timebase", {})
    require(tb.get("numer", 0) > 0, "mach_timebase.numer must be > 0")
    require(tb.get("denom", 0) > 0, "mach_timebase.denom must be > 0")
    require(
        tb.get("two_sample_smoke_check_nondecreasing") is True,
        "mach_timebase.two_sample_smoke_check_nondecreasing must be true",
    )

    rl = d.get("setrlimit_nofile", {})
    require(rl.get("soft_before", 0) > 0, "setrlimit_nofile.soft_before must be > 0")
    require(rl.get("hard_before", 0) > 0, "setrlimit_nofile.hard_before must be > 0")

    hv = d.get("hypervisor", {})
    require("hv_vm_create_hr" in hv, "hypervisor.hv_vm_create_hr must be present")
    if hv.get("hv_vm_create_ok"):
        require(
            hv.get("hv_vm_get_max_vcpu_count_ok") is True,
            "hypervisor.hv_vm_get_max_vcpu_count_ok must be true when "
            "hv_vm_create_ok is true",
        )
        require(
            hv.get("vcpu_attempted") is True,
            "hypervisor.vcpu_attempted must be true when hv_vm_create_ok is true",
        )
    require(
        not (hv.get("crashed") is False and hv.get("crash_signal", 0) != 0),
        "hypervisor.crash_signal must be 0 when crashed is false",
    )
    # A call reported not-attempted must carry null, never a return-code
    # look-alike (REV-039 C5).
    if not hv.get("hv_vm_get_max_vcpu_count_attempted"):
        require(
            hv.get("hv_vm_get_max_vcpu_count_hr") is None,
            "hypervisor.hv_vm_get_max_vcpu_count_hr must be null when not attempted",
        )
    if not hv.get("vcpu_attempted"):
        require(
            hv.get("hv_vcpu_create_hr") is None,
            "hypervisor.hv_vcpu_create_hr must be null when vcpu not attempted",
        )
    if not hv.get("hv_vcpu_get_sys_reg_id_aa64mmfr0_el1_attempted"):
        require(
            hv.get("hv_vcpu_get_sys_reg_id_aa64mmfr0_el1_hr") is None,
            "hypervisor.hv_vcpu_get_sys_reg_id_aa64mmfr0_el1_hr must be null "
            "when not attempted",
        )

    # --- KA-relevant REQUIRED outcomes (REV-039 C1 / REV-038 M6c) ---
    # Internal consistency alone lets a run in which every capability failed
    # PASS. A non-negative run must also demonstrate the capability KA-01
    # depends on, and — keyed on the invocation's own label — the documented
    # Hypervisor entitlement discriminator KA-02's F3 input depends on.
    if not negative:
        require(mj.get("mmap_ok") is True, "map_jit.mmap_ok must be true (KA-01)")
        require(
            jwp.get("toggle_ok") is True and jwp.get("crashed") is False,
            "jit_write_protect.toggle_ok must be true and crashed false (KA-01)",
        )
        if label == "unentitled":
            require(
                hv.get("hv_vm_create_hr") == HV_DENIED,
                f"hypervisor.hv_vm_create_hr must be {HV_DENIED} for the "
                "unentitled label",
            )
        elif label == "entitled":
            require(
                hv.get("hv_vm_create_hr") == HV_SUCCESS,
                f"hypervisor.hv_vm_create_hr must be {HV_SUCCESS} for the "
                "entitled label",
            )

    return failures


def main(argv):
    if len(argv) != 2:
        print("usage: check.py <probe-output.json>", file=sys.stderr)
        return 2
    with open(argv[1]) as f:
        d = json.load(f)
    failures = check(d)
    if failures:
        print(f"FAIL ({len(failures)} rule(s) violated):")
        for f_ in failures:
            print(f"  - {f_}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

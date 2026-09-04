#!/usr/bin/env python3
"""Validate one INV-01-01 probe JSON output against the invariants that a
truthful run must satisfy. Standard library only.

Usage: check.py <path-to-probe-output.json>
Exit 0 and print PASS if every rule holds; exit 1 and print each failing
rule otherwise. This is the code-gate validation tool: it must FAIL on the
probe's own `--negative` output, which is exercised by run.sh before any
evidence run and recorded as the negative-control result, not a claim.
"""
import json
import sys


def check(d):
    failures = []

    def require(cond, msg):
        if not cond:
            failures.append(msg)

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
            jwp.get("write_with_callback_np_value") == jwp.get("expected_value", 42),
            "jit_write_protect.write_with_callback_np_value must equal "
            "expected_value when write_with_callback_np_ok is true",
        )

    remap = d.get("mach_vm_remap", {})
    if remap.get("ok"):
        require(
            remap.get("alias_write_visible") is True,
            "mach_vm_remap.alias_write_visible must be true when ok is true",
        )

    tb = d.get("mach_timebase", {})
    require(tb.get("numer", 0) > 0, "mach_timebase.numer must be > 0")
    require(tb.get("denom", 0) > 0, "mach_timebase.denom must be > 0")
    require(
        tb.get("monotonic_nondecreasing") is True,
        "mach_timebase.monotonic_nondecreasing must be true",
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
    if hv.get("hv_vcpu_get_sys_reg_id_aa64mmfr0_el1_ok"):
        for field in (
            "id_aa64mmfr0_el1_parange",
            "id_aa64mmfr0_el1_tgran4",
            "id_aa64mmfr0_el1_tgran16",
            "id_aa64mmfr0_el1_tgran64",
        ):
            v = hv.get(field)
            require(
                isinstance(v, int) and 0 <= v <= 15,
                f"hypervisor.{field} must be a 4-bit field (0-15) when the "
                "sysreg read succeeded",
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

#!/usr/bin/env python3
"""Validate one INV-01-14 probe JSON output against the invariants a
truthful run must satisfy. Standard library only.

Usage: check.py <path-to-probe-output.json>
Exit 0 and print PASS if every rule holds; exit 1 and print each failing
rule otherwise. This is the code-gate validation tool: it must FAIL on the
probe's own `--negative` output, which run.sh exercises before any evidence
run and records as the negative-control result, not a claim.
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
    require(host.get("hw_memsize", 0) > 0, "host.hw_memsize must be > 0")

    nf = d.get("rlimit_nofile", {})
    require(nf.get("set_ok") is True, "rlimit_nofile.set_ok must be true")
    if nf.get("set_ok"):
        require(
            nf.get("soft_after_set") == 8,
            "rlimit_nofile.soft_after_set must equal the requested 8",
        )

    cpu = d.get("rlimit_cpu", {})
    require(cpu.get("set_ok") is True, "rlimit_cpu.set_ok must be true")
    # setrlimit(2) (SRC-035) documents RLIMIT_CPU as delivering SIGXCPU when
    # exceeded; a 1 s limit under a busy loop must eventually deliver it.
    # This is the invariant the --negative mode violates by fabricating false.
    require(
        cpu.get("sigxcpu_delivered") is True,
        "rlimit_cpu.sigxcpu_delivered must be true: exceeding RLIMIT_CPU "
        "must deliver SIGXCPU",
    )

    ras = d.get("rlimit_as", {})
    require("mmap_ok" in ras, "rlimit_as.mmap_ok must be present")
    rdata = d.get("rlimit_data", {})
    require("malloc_ok" in rdata, "rlimit_data.malloc_ok must be present")

    mp = d.get("memorypressure_source", {})
    if mp.get("create_ok"):
        require(
            mp.get("resume_ok") is True,
            "memorypressure_source.resume_ok must be true when create_ok is true",
        )

    require(
        d.get("mmap_touch_1gib_ok") is True,
        "mmap_touch_1gib_ok must be true (a 1 GiB anonymous mapping must succeed)",
    )

    before = d.get("task_vm_info_before", {})
    after = d.get("task_vm_info_after_touch_1gib", {})
    require(before.get("ok") is True, "task_vm_info_before.ok must be true")
    require(after.get("ok") is True, "task_vm_info_after_touch_1gib.ok must be true")
    if before.get("ok") and after.get("ok"):
        require(
            isinstance(before.get("phys_footprint"), int)
            and isinstance(after.get("phys_footprint"), int),
            "phys_footprint counters must be integers",
        )
        require(
            isinstance(before.get("region_count"), int)
            and isinstance(after.get("region_count"), int),
            "region_count counters must be integers",
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
    else:
        print("PASS")

    # These are predeclared Hypothesis outcomes, not measurement-integrity
    # rules. A MISS is evidence for result review and must not invalidate the
    # otherwise truthful sample.
    before = d.get("task_vm_info_before", {})
    after = d.get("task_vm_info_after_touch_1gib", {})
    footprint_meets = d.get("increase_at_least_800mib") is True
    regions_meet = (
        isinstance(before.get("region_count"), int)
        and isinstance(after.get("region_count"), int)
        and after["region_count"] > before["region_count"]
    )
    print("-- decision thresholds (result gate adjudicates) --")
    print(
        f"{'MEET' if footprint_meets else 'MISS'} "
        "T1 phys_footprint increase >= 800 MiB"
    )
    print(
        f"{'MEET' if regions_meet else 'MISS'} "
        "T2 region_count increases after the additional mapping"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

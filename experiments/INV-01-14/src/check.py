#!/usr/bin/env python3
"""Validate one INV-01-14 probe JSON output against the invariants a
truthful run must satisfy. Standard library only.

The probe emits newline-delimited JSON: one self-contained object per line
(a "header" line, then one line per check). This checker reads line by
line rather than json.load()-ing the whole file, so a process death
partway through a run leaves a file of N valid, parseable lines rather than
one truncated object that would raise a bare exception here (REV-048
required change 8). A missing later line is reported as a specific failure
("check <name> is missing"), not a crash.

Usage: check.py <path-to-probe-output.json>
Exit 0 and print PASS if every rule holds; exit 1 and print each failing
rule otherwise. This is the code-gate validation tool: it must FAIL on the
probe's own `--negative` output, which run.sh exercises before any evidence
run and records as the negative-control result, not a claim.
"""
import json
import sys

EXPECTED_CHECKS = (
    "header", "rlimit_nofile", "rlimit_cpu", "rlimit_as_data",
    "memorypressure_source", "task_vm_info",
)


def load_lines(path):
    """Parse each non-blank line as its own JSON object. A line that fails
    to parse (a partial write cut off mid-line by a process death) is
    dropped rather than raised, since everything before it is still valid
    evidence of how far the run got."""
    objs = []
    parse_errors = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                objs.append(json.loads(line))
            except json.JSONDecodeError:
                parse_errors += 1
    return objs, parse_errors


def merge(objs):
    d = {}
    seen = set()
    for obj in objs:
        seen.add(obj.get("check"))
        for k, v in obj.items():
            if k != "check":
                d[k] = v
    return d, seen


def check(d, seen, parse_errors):
    failures = []

    def require(cond, msg):
        if not cond:
            failures.append(msg)

    require(parse_errors == 0,
            f"{parse_errors} line(s) failed to parse as JSON")
    for name in EXPECTED_CHECKS:
        require(name in seen, f"check '{name}' is missing from the output")

    require(d.get("experiment") == "INV-01-14", "experiment must be INV-01-14")
    require(d.get("mode") in ("normal", "negative", "fixture-valid", "fixture-negative"),
            "mode must identify a declared probe or fixture mode")

    host = d.get("host", {})
    require(host.get("hw_pagesize", 0) > 0, "host.hw_pagesize must be > 0")
    require(host.get("hw_ncpu", 0) >= 1, "host.hw_ncpu must be >= 1")
    require(host.get("hw_memsize", 0) > 0, "host.hw_memsize must be > 0")

    nf = d.get("rlimit_nofile", {})
    require(nf.get("getrlimit_before_ok") is True,
            "rlimit_nofile.getrlimit_before_ok must be true")
    require(nf.get("set_ok") is True, "rlimit_nofile.set_ok must be true")
    if nf.get("set_ok"):
        require(
            nf.get("soft_after_set") == 8,
            "rlimit_nofile.soft_after_set must equal the requested 8",
        )
    # emfile_observed is the descriptor-enforcement metric this record's
    # Metrics table declares (REV-047/REV-048 required changes 6/4): a
    # lowered soft limit that is set but never actually enforced must FAIL,
    # not silently PASS because the checker never looked at the field.
    require(nf.get("emfile_observed") is True,
            "rlimit_nofile.emfile_observed must be true: the lowered "
            "descriptor limit must actually be enforced")

    cpu = d.get("rlimit_cpu", {})
    require(cpu.get("getrlimit_before_ok") is True,
            "rlimit_cpu.getrlimit_before_ok must be true")
    require(cpu.get("set_ok") is True, "rlimit_cpu.set_ok must be true")
    # setrlimit(2) (SRC-035) documents RLIMIT_CPU as delivering SIGXCPU when
    # exceeded; a 1 s limit under a busy loop must eventually deliver it.
    # This is the invariant the --negative mode violates by fabricating false.
    require(
        cpu.get("sigxcpu_delivered") is True,
        "rlimit_cpu.sigxcpu_delivered must be true: exceeding RLIMIT_CPU "
        "must deliver SIGXCPU",
    )

    # RLIMIT_AS/RLIMIT_DATA: CLM-01-14-10 asks whether setrlimit succeeds in
    # lowering the soft limit *below the process's current default*, which
    # is unanswerable without the prior limit in the schema (REV-047/
    # REV-048 required changes 12/12). set_ok/set_errno and the read-back
    # soft_after_set are recorded but not asserted to a specific value: the
    # method's own open question (CLM-01-14-10) is exactly whether Darwin
    # supports lowering these limits, so a checker requirement here would
    # decide the question the evidence run exists to answer.
    ras = d.get("rlimit_as", {})
    require(ras.get("getrlimit_before_ok") is True,
            "rlimit_as.getrlimit_before_ok must be true")
    require("set_ok" in ras and "set_errno" in ras,
            "rlimit_as must report set_ok and set_errno")
    require("mmap_ok" in ras, "rlimit_as.mmap_ok must be present")
    rdata = d.get("rlimit_data", {})
    require(rdata.get("getrlimit_before_ok") is True,
            "rlimit_data.getrlimit_before_ok must be true")
    require("set_ok" in rdata and "set_errno" in rdata,
            "rlimit_data must report set_ok and set_errno")
    require("malloc_ok" in rdata, "rlimit_data.malloc_ok must be present")

    mp = d.get("memorypressure_source", {})
    require("create_ok" in mp, "memorypressure_source.create_ok must be present")

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
    objs, parse_errors = load_lines(argv[1])
    d, seen = merge(objs)
    failures = check(d, seen, parse_errors)
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

#!/usr/bin/env python3
"""REV-082 independent replication checker for INV-01-14.

Written by reviewer context `t01_machine_reviewer`. Not the author's
`src/check.py`, and deliberately sharing no code with it: the invariants are
re-derived here from the raw NDJSON, the method of record, and SRC-035, so
agreement between the two checkers is evidence rather than a shared bug.
Standard library only.

Usage: verify.py <arm-label> <probe-output.ndjson>
Exit 0 = every invariant holds, 1 = at least one violated, 2 = usage/parse.
"""
import json
import sys

EMFILE = 24
RLIM_INFINITY = (1 << 63) - 1
EXPECTED = ("header", "rlimit_nofile", "rlimit_cpu", "rlimit_as_data",
            "memorypressure_source", "task_vm_info")
MIB = 1024 * 1024


def read_ndjson(path):
    """One self-contained object per line. A line that will not parse is
    counted, not raised: a process death mid-run must leave the preceding
    lines readable."""
    objs, bad = [], 0
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                objs.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    merged, seen = {}, set()
    for o in objs:
        seen.add(o.get("check"))
        merged.update({k: v for k, v in o.items() if k != "check"})
    return merged, seen, bad


def invariants(d, seen, bad):
    nf = d.get("rlimit_nofile", {})
    cpu = d.get("rlimit_cpu", {})
    ras = d.get("rlimit_as", {})
    rda = d.get("rlimit_data", {})
    before = d.get("task_vm_info_before", {})
    after = d.get("task_vm_info_after_touch_1gib", {})

    yield bad == 0, "every line parsed as JSON"
    for name in EXPECTED:
        yield name in seen, f"check '{name}' present"
    yield d.get("experiment") == "INV-01-14", "experiment tag is INV-01-14"
    yield d.get("host", {}).get("hw_memsize") == 17179869184, "host has 16 GiB"
    yield d.get("host", {}).get("hw_ncpu") == 10, "host has 10 CPUs"

    # Descriptors: SRC-035 documents a per-process open-file limit. The
    # lowered limit must actually be enforced, not merely accepted.
    yield nf.get("getrlimit_before_ok") is True, "NOFILE prior limit was read"
    yield nf.get("set_ok") is True, "NOFILE soft limit was lowered"
    yield nf.get("soft_after_set") == 8, "NOFILE read-back limit is 8"
    yield nf.get("emfile_observed") is True, "NOFILE limit enforced (EMFILE seen)"
    yield nf.get("errno_at_failure") == EMFILE, "failure errno is EMFILE"
    # 3 standard descriptors plus the opened ones must not exceed the limit.
    oc = nf.get("opened_count")
    yield isinstance(oc, int) and 0 <= oc <= 8, "opened_count fits under the limit"

    # CPU: SRC-035 documents SIGXCPU on exceeding the soft limit.
    yield cpu.get("getrlimit_before_ok") is True, "CPU prior limit was read"
    yield cpu.get("set_ok") is True, "CPU soft limit was lowered"
    yield cpu.get("soft_after_set") == 1, "CPU read-back limit is 1 s"
    yield cpu.get("sigxcpu_delivered") is True, "SIGXCPU was delivered"

    # AS/DATA: CLM-01-14-10 is open, so nothing is asserted about set_ok.
    # What IS asserted is that the artifact records enough to interpret it.
    for name, r in (("rlimit_as", ras), ("rlimit_data", rda)):
        yield r.get("getrlimit_before_ok") is True, f"{name} prior limit was read"
        yield "set_ok" in r and "set_errno" in r, f"{name} records set outcome"
        yield "soft_before" in r and "hard_before" in r and "soft_after_set" in r, \
            f"{name} records prior and read-back limits"
        # If the set failed, the read-back must show the limit did not move --
        # otherwise 'refused' and 'silently clamped' are indistinguishable.
        if r.get("set_ok") is False:
            yield r.get("soft_after_set") == r.get("soft_before"), \
                f"{name} soft limit unchanged after a refused set"
            yield r.get("limit_bytes", 0) < r.get("hard_before", 0), \
                f"{name} request was below the prior hard limit"

    yield d.get("memorypressure_source", {}).get("create_ok") is True, \
        "memory-pressure source was created"

    # task_info: recompute the increase rather than trusting the probe.
    yield d.get("mmap_touch_1gib_ok") is True, "1 GiB anonymous mapping succeeded"
    pf_b, pf_a = before.get("phys_footprint"), after.get("phys_footprint")
    rc_b, rc_a = before.get("region_count"), after.get("region_count")
    yield all(isinstance(x, int) for x in (pf_b, pf_a, rc_b, rc_a)), \
        "task_vm_info counters are integers"
    if all(isinstance(x, int) for x in (pf_b, pf_a)):
        delta = pf_a - pf_b
        yield delta == d.get("phys_footprint_increase_bytes"), \
            "reported phys_footprint increase equals after-minus-before"
        yield (delta >= 800 * MIB) is (d.get("increase_at_least_800mib") is True), \
            "increase_at_least_800mib agrees with the recomputed delta"
    if all(isinstance(x, int) for x in (rc_b, rc_a)):
        yield rc_a > rc_b, "region_count increased after the additional mapping"


def main(argv):
    if len(argv) != 3:
        print("usage: verify.py <arm-label> <probe-output.ndjson>", file=sys.stderr)
        return 2
    label = argv[1]
    try:
        d, seen, bad = read_ndjson(argv[2])
    except OSError as e:
        print(f"{label}: PARSE-ERROR {e}", file=sys.stderr)
        return 2
    violated = [why for ok, why in invariants(d, seen, bad) if not ok]
    for why in violated:
        print(f"{label}: VIOLATED {why}")
    print(f"{label}: {'FAIL' if violated else 'PASS'} ({len(violated)} violated)")
    # Reported, never gating: these are the record's Hypothesis thresholds.
    if isinstance(d.get("phys_footprint_increase_bytes"), int):
        print(f"{label}: observed phys_footprint delta "
              f"{d['phys_footprint_increase_bytes']} bytes "
              f"({d['phys_footprint_increase_bytes'] / MIB:.3f} MiB)")
    return 1 if violated else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

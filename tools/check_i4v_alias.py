#!/usr/bin/env python3
"""Check finite N-vCPU high/low alias pressure without erasing address errors.

Every normalized address is first checked against its exact declared phase.
Only named address roots are then lowered in a copy; the existing independent
I4 oracle still checks arithmetic, full state, event packets, ownership, FIFO,
bounded progress, retained routes/code, stale replies and reclamation.
"""

import argparse
import copy
import json
from pathlib import Path

import check_i4
from check_i1 import state, values
from i4v_check import HIGH, IPA, PRIVATE, SOURCE, TCR, exact, require, uint

DATA = 0x100000


def address(cpu, phase, displacement=0):
    return (HIGH if (cpu + phase) % 2 == 0 else 0) + DATA + cpu * 256 + displacement


def checked_cpu(item, n):
    cpu = uint(item["id"], 32, "CPU id")
    require(cpu < n, "unconfigured participant")
    return cpu


def replace_address(item, key, expected, lowered, label):
    exact(item[key], expected, label)
    item[key] = lowered


def normalize_frame(frame, cpu, phase, label):
    state(frame, label)
    for reg, displacement in ((6, 0), (11, 128)):
        replace_address(frame["x"], reg, address(cpu, phase, displacement), DATA + cpu * 256 + displacement,
                        f"{label}.x[{reg}] phase {phase}")


def normalize_cell(cell, n, phase):
    cpu = checked_cpu(cell, n)
    for field in ("initial", "captured"):
        normalize_frame(cell[field], cpu, phase, f"CPU{cpu}.{field}")
    replace_address(cell, "memory_base", address(cpu, phase), DATA + cpu * 256, "guest memory base")
    if "dirty_fault" in cell:
        normalize_frame(cell["dirty_fault"]["frame"], cpu, phase, f"CPU{cpu}.dirty fault")
        replace_address(cell["dirty_fault"], "far", address(cpu, phase, 128), DATA + cpu * 256 + 128,
                        "actual native store FAR")


def normalize(record):
    """Return a copy after asserting every address change allowed by the fixture."""
    require(type(record) is dict, "expected alias pressure report")
    profile = uint(record["alias_profile"], 32, "alias_profile")
    require(profile in (1, 2), "alias profile must select low/high guest source")
    exact(record["profile"], "i4-alias-low" if profile == 1 else "i4-alias-high", "alias pressure profile")
    source = SOURCE + (HIGH if profile == 2 else 0)
    exact(record["source_rip"], source, "compiled guest source RIP")
    n = uint(record["configured_vcpus"], 32, "configured_vcpus")
    require(1 <= n <= 4, "finite alias participant range")
    result = copy.deepcopy(record)
    result["profile"] = "i4-pressure"
    for name, phase in (("cells", 0), ("new_cells", 1)):
        require(type(result[name]) is list and len(result[name]) == n, "one execution per participant")
        for cell in result[name]:
            normalize_cell(cell, n, phase)
    require(type(result["faults"]) is list and len(result["faults"]) == n, "one initial fault per participant")
    for fault in result["faults"]:
        cpu = checked_cpu(fault, n)
        normalize_frame(fault["frame"], cpu, 0, f"CPU{cpu}.initial fault")
        replace_address(fault, "far", address(cpu, 0), DATA + cpu * 256, "actual initial native FAR")
    intervals = result["maintenance"]["intervals"]
    require(type(intervals) is list and 0 < len(intervals) <= 4 * n, "bounded ordinary intervals")
    ordinals = [uint(cell["ordinal"], 64, "interval ordinal") for cell in intervals]
    exact(sorted(ordinals), list(range(1, len(intervals) + 1)), "complete unique interval ordering")
    counts = [0] * n
    for cell in sorted(intervals, key=lambda item: item["ordinal"]):
        cpu = checked_cpu(cell, n)
        normalize_cell(cell, n, counts[cpu] + 2)
        counts[cpu] += 1
    require(type(result["events"]) is list and len(result["events"]) == n, "one event result per participant")
    for event in result["events"]:
        cpu = checked_cpu(event, n)
        phase = 0 if cpu in (1, 3) else 1
        packet = event["packet"]
        values(packet["gpr"], 16, 64, "canonical event GPRs")
        for reg, displacement in ((3, 0), (7, 128)):
            replace_address(packet["gpr"], reg, address(cpu, phase, displacement), DATA + cpu * 256 + displacement,
                            "canonical event address root")
        offset = 0 if phase == 0 else 17
        replace_address(packet, "rip", source + offset, SOURCE + offset, "canonical event RIP")
    require(type(result["requests"]) is list and len(result["requests"]) == 3, "three producer requests")
    for request in result["requests"]:
        values(request, 16, 64, "producer request")
        replace_address(request, 12, source, SOURCE, "producer guest RIP")
        replace_address(request, 10, 3, 1, "producer reply profile binds full RIP")
    return result


def descriptor(page, readonly=True):
    return (IPA + 0x70000 + page * 4096) | 3 | (1 << 10) | (3 << 8) | (3 << 53) | (int(readonly) << 7)


def expected_aliases(record, n):
    """Replay only route-changing events; the base oracle validates their order.

    Each acknowledged remap installs both aliases read-only. Native dirty repair
    makes only the currently executing alias writable. No address normalization
    is applied to these native physical descriptors or acknowledgement records.
    """
    profile = record["alias_profile"]
    leaves = [[0, 0] for _ in range(n)]
    warm, acknowledgements = [], [[] for _ in range(n)]
    for cpu in range(n):
        inactive = 0 if (cpu % 2) == 0 else 1
        leaves[cpu][inactive] = descriptor(0)
        warm.append(list(leaves[cpu]))
    total = 0
    ordinary_counts = [0] * n
    for word in record["trace"]:
        event, cpu = word >> 8, word & 255
        require(cpu < n, "route event outside participant set")
        if event == 9:
            route = descriptor(1)
            leaves[cpu] = [route, route]
            acknowledgements[cpu].append([2, route, route])
        elif event == 15:
            active = int((cpu + 1) % 2 == 0)
            leaves[cpu][active] = descriptor(1, False)
        elif event == 26:
            route = descriptor(int((total + 1) % 2 == 0))
            leaves[cpu] = [route, route]
            acknowledgements[cpu].append([3 + total, route, route])
        elif event == 27:
            total += 1
        elif event == 31:
            active = int((cpu + ordinary_counts[cpu] + 2) % 2 == 0)
            leaves[cpu][active] = descriptor(int(total % 2 == 0), False)
        elif event == 30:
            ordinary_counts[cpu] += 1
    expected = []
    for cpu in range(n):
        root, l1, l2, lower, upper = ([0] * 512 for _ in range(5))
        physical_base = IPA + cpu * 0x100000
        root[256], l1[0], l2[0] = [physical_base + offset + 3 for offset in (0x18000, 0x19000, 0x1A000)]
        lower[256], upper[256] = leaves[cpu]
        writer = cpu == 0
        if writer:
            (upper if profile == 1 else lower)[258] = descriptor(2, False)
        acks = acknowledgements[cpu]
        require(len(acks) == 1 + 9 * n, "one composed and every maintenance acknowledgement per owner")
        expected.append({"id": cpu, "current_el": 4, "tcr": TCR,
                         "ttbr1": physical_base + 0x17000, "warm": warm[cpu],
                         "ack_count": len(acks), "acks": acks + [[0] * 3 for _ in range(37 - len(acks))],
                         "wrong_rip_rejections": int(writer), "blank_before_publish": int(writer),
                         "source_address": SOURCE + (HIGH if profile == 1 else 0) if writer else 0,
                         "source_gpa": SOURCE if writer else 0,
                         "source_leaf_before": SOURCE | 0x27 if writer else 0,
                         "source_leaf_after": SOURCE | 0x67 if writer else 0,
                         "source_before": list(check_i4.X86_XOR) if writer else [0] * 17,
                         "source_after": list(check_i4.X86_OR) if writer else [0] * 17,
                         "upper_root": root, "upper_l1": l1, "upper_l2": l2,
                         "upper_leaf": upper, "lower_leaf": lower})
    return expected


def check(record, expected_vcpus=None, expected_delay=None, expected_alias_profile=None):
    normalized = normalize(record)
    n = record["configured_vcpus"]
    if expected_alias_profile is not None:
        exact(record["alias_profile"], expected_alias_profile, "requested source-address profile")
    check_i4.check(normalized, expected_vcpus, expected_delay, "i4-pressure")
    aliases = record["aliases"]
    require(type(aliases) is list and len(aliases) == n, "one alias snapshot per owner")
    expected = expected_aliases(record, n)
    seen = set()
    for item in aliases:
        cpu = checked_cpu(item, n)
        require(cpu not in seen, "duplicate alias snapshot")
        seen.add(cpu)
        require(uint(item["sctlr"], 64, "sctlr") & 0x1005 == 0x1005, "MMU/cache profile disabled")
        require(uint(item["actlr"], 64, "actlr") & 2 == 2, "TSO profile disabled")
        values_to_check = {key: value for key, value in item.items() if key not in ("sctlr", "actlr")}
        exact(values_to_check, expected[cpu], f"CPU{cpu} alias lifetime and tables")
    exact(seen, set(range(n)), "alias participant set")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--vcpus", type=int)
    parser.add_argument("--delay-ms", type=int)
    parser.add_argument("--alias-profile", type=int, choices=(1, 2))
    args = parser.parse_args()
    for path in args.paths:
        try:
            check(json.loads(path.read_text()), args.vcpus, args.delay_ms, args.alias_profile)
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise SystemExit(f"{path}: FAIL: {error}") from error
        print(f"{path}: PASS (finite N-vCPU alias pressure profile only)")


if __name__ == "__main__":
    main()

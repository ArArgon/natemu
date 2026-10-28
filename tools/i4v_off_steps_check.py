#!/usr/bin/env python3
"""Independent finite oracle for protected32 single-instruction FEX execution.

The source is the retained upstream moffs case. Each instruction is separately
compiled; no whole-block guest/native recovery boundaries are inferred. Literal
segment, A20, device/hole and memory expectations are separate from native roots.
"""

import argparse
import copy
import json
from pathlib import Path

from check_i1 import resident_pc
from i4v_check import CODE, DATA, IPA, PRIVATE, TCR, exact, require, uint, zero_frame
from i4v_off_check import SOURCE_BYTES, GUEST_OFFSETS, RETURN, native_seed, guest_seed as flat_guest_seed, architectural_effects, zeros

EIP = 0x2000
SCENARIOS = ("segments", "a20-off", "ds-limit", "cs-before-load", "cs-inside-load", "cs-first",
             "ds-readonly", "device", "hole", "linear-wrap")
BODIES = tuple(bytes.fromhex(body) for body in (
    "0502a052", "44088052", "1f2003d5a4fc9f88", "04008012",
    "1402a05284fedf881f2003d5", "e50304aa", "04008012",
    "1402a05294fedf481f2003d5843e40b3", "e70304aa"))
MEMORY_STEPS = (2, 4, 7)
WIDTHS = {2: 4, 4: 4, 7: 2}


def profile(scenario):
    require(scenario in SCENARIOS, "unknown protected32 step scenario")
    ds_base = {"device": 0x400000, "hole": 0x500000, "linear-wrap": 0xFFF00000}.get(scenario, 0x200000)
    a20 = int(scenario != "a20-off")
    linear = (ds_base + DATA) & 0xFFFFFFFF
    return {"id": SCENARIOS.index(scenario), "a20": a20, "cs_base": 0x300000, "ds_base": ds_base,
            "cs_limit": {"cs-before-load": 0x2010, "cs-inside-load": 0x2013, "cs-first": 0x1FFF}.get(scenario, 0xFFFFFFFF),
            "ds_limit": 0xFFFFF if scenario == "ds-limit" else 0xFFFFFFFF,
            "cs_attrs": 0x409B if scenario.startswith("cs-") else 0xC09B,
            "ds_attrs": 0x4093 if scenario == "ds-limit" else 0xC091 if scenario == "ds-readonly" else 0xC093,
            "linear": linear, "gpa": linear if a20 else linear & ~(1 << 20),
            "source_gpa": 0x302000 if a20 else 0x202000,
            "completed": {"ds-limit": 2, "ds-readonly": 2, "cs-before-load": 4,
                          "cs-inside-load": 4, "cs-first": 0}.get(scenario, 9)}


def mutation_site(mutation):
    require(type(mutation) is int and -1 <= mutation <= 24, "invalid step reply mutation")
    return (8, mutation) if 0 <= mutation < 16 else (mutation - 16, 0) if mutation >= 16 else None


def guest_values(initial_guest, completed, hole=False):
    registers = {name: initial_guest["gpr"][index] for name, index in (("RAX", 0), ("RCX", 1), ("RDX", 2))}
    # Ordinary RAM and the bounded device both retain the dword store's value.
    values, _ = architectural_effects(registers, dict(enumerate([0xA5] * 4, DATA)), completed)
    if hole:
        # The declared machine hole discards the store and returns all ones.
        if completed >= 5:
            values["RAX"] = 0xFFFFFFFF
        if completed >= 6:
            values["RDX"] = 0xFFFFFFFF
        if completed >= 9:
            values["RCX"] = 0xFFFFFFFF
    return values


def native_step(initial, step, code, capture, hole=False, fault=False):
    """Literal per-instruction native roots, preserving every unrelated field."""
    frame = copy.deepcopy(initial)
    frame["x"][16], frame["x"][30] = code, capture
    if step == 0:
        frame["x"][5] = DATA
    elif step == 1:
        frame["x"][4] = 0x42
    elif step in (3, 6):
        frame["x"][4] = 0xFFFFFFFF
    elif step == 4:
        frame["x"][20] = DATA
        if not fault:
            frame["x"][4] = 0xFFFFFFFF if hole else 0x42
    elif step == 5:
        frame["x"][5] = frame["x"][4]
    elif step == 7:
        frame["x"][20] = DATA
        if not fault:
            frame["x"][20] = 0xFFFF if hole else 0x42
            frame["x"][4] = (frame["x"][4] & ~0xFFFF) | frame["x"][20]
    elif step == 8:
        frame["x"][7] = frame["x"][4]
    return frame


def request_packet(vm, scenario, step):
    selected = profile(scenario)
    source = SOURCE_BYTES[GUEST_OFFSETS[step]:GUEST_OFFSETS[step + 1]]
    eip = EIP + GUEST_OFFSETS[step]
    linear = selected["cs_base"] + eip
    gpa = linear if selected["a20"] else linear & ~(1 << 20)
    return [0x49345631, 1, vm, 0, 1, 1, selected["id"] + 1, eip, 32,
            int.from_bytes(source.ljust(8, b"\0"), "little"), len(source), linear, gpa,
            step + 1, len(BODIES[step]), step]


def reply_packet(vm, scenario, step):
    reply = request_packet(vm, scenario, step)
    padded = BODIES[step].ljust(16, b"\0")
    reply[0] = 0x49345632
    reply[9:11] = [int.from_bytes(padded[index:index + 8], "little") for index in (0, 8)]
    return reply


def guest_seed(scenario):
    selected = profile(scenario)
    initial, _ = native_seed()
    guest = flat_guest_seed(initial, EIP)
    guest["segments"][0] = [8, selected["cs_base"], selected["cs_limit"], selected["cs_attrs"]]
    guest["segments"][1] = [0x10, selected["ds_base"], selected["ds_limit"], selected["ds_attrs"]]
    return guest


def guest_after(scenario, completed):
    guest = guest_seed(scenario)
    values = guest_values(guest, completed, scenario == "hole")
    for name, index in (("RAX", 0), ("RCX", 1), ("RDX", 2)):
        guest["gpr"][index] = values[name]
    guest["rip"] = EIP + GUEST_OFFSETS[completed]
    return guest


def data_pages(scenario, completed):
    pages = [[byte] * 4096 for byte in (0xA5, 0x5A, 0x3C)]
    if completed > 2 and scenario not in ("device", "hole"):
        page = {"a20-off": 1, "linear-wrap": 2}.get(scenario, 0)
        pages[page][:4] = [0x42, 0, 0, 0]
    return pages


def source_pages(scenario):
    pages = [[fill] * 4096 for fill in (0xCC, 0x33)]
    active = int(scenario == "a20-off")
    pages[active][:len(SOURCE_BYTES)] = SOURCE_BYTES
    return pages


def code_image(completed):
    image = [0] * (32 * 9)
    for step in range(completed):
        instructions = BODIES[step] + RETURN
        image[step * 32:step * 32 + len(instructions)] = instructions
    return image


def execution_prefixes(scenario, completed, capture):
    frame, _ = native_seed()
    prefixes, faults = [], []
    for step in range(completed):
        code = CODE + step * 32
        if step in MEMORY_STEPS:
            faults.append(native_step(frame, step, code, capture, scenario == "hole", fault=True))
        frame = native_step(frame, step, code, capture, scenario == "hole")
        prefixes.append(copy.deepcopy(frame))
    return prefixes, faults


def expected_progress(scenario, mutation, nonce, capture):
    selected = profile(scenario)
    bad = mutation_site(mutation)
    require(bad is None or scenario == "segments", "reply controls use the complete RAM profile")
    completed = bad[0] if bad else selected["completed"]
    requested = completed + int(bad is not None)
    faulted = completed < 9 and bad is None
    # A DS rejection follows a successful fetch of that current instruction.
    fetched = requested + int(faulted and scenario in ("ds-limit", "ds-readonly"))
    prefixes, native_faults = execution_prefixes(scenario, completed, capture)
    actual_steps = [step for step in MEMORY_STEPS if step < completed]
    fetch_linear, fetch_gpa, data_linear, data_gpa = ([0] * 9 for _ in range(4))
    for step in range(fetched):
        fetch_linear[step] = selected["cs_base"] + EIP + GUEST_OFFSETS[step]
        fetch_gpa[step] = selected["source_gpa"] + GUEST_OFFSETS[step]
    for step in MEMORY_STEPS:
        if step < requested:
            data_linear[step], data_gpa[step] = selected["linear"], selected["gpa"]
    requests = [request_packet(nonce, scenario, step) for step in range(requested)]
    replies = [reply_packet(nonce, scenario, step) for step in range(requested)]
    if bad:
        replies[-1][bad[1]] ^= 1
    services, before_retry = [], []
    serviced = scenario in ("device", "hole")
    for step in actual_steps:
        if serviced:
            value = 0x42 if step == 2 or scenario == "device" else (1 << (WIDTHS[step] * 8)) - 1
            services.append([step, int(step == 2), WIDTHS[step], selected["linear"], selected["gpa"], value])
            before_retry.append([0] * 16)
        else:
            page = {"a20-off": 1, "linear-wrap": 2}.get(scenario, 0)
            before_retry.append(data_pages(scenario, step)[page][:16])
    guest_prefixes = []
    for count in range(1, completed + 1):
        guest = guest_after(scenario, count)
        guest_prefixes.append([*guest["gpr"][:3], guest["rip"]])
    steps = {"profile": selected["id"], "a20": selected["a20"], "transfers": requested,
             "step": min(completed, 8), "entries": [1] * completed + [0] * (9 - completed),
             "requests": requests + [[0] * 16 for _ in range(9 - requested)],
             "replies": replies + [[0] * 16 for _ in range(9 - requested)],
             "fetch_linear": fetch_linear, "fetch_gpa": fetch_gpa, "data_linear": data_linear, "data_gpa": data_gpa,
             "prefixes": prefixes + [zero_frame() for _ in range(9 - completed)],
             "guest_prefixes": guest_prefixes + [[0] * 4 for _ in range(9 - completed)],
             "fault_steps": actual_steps + [0] * (3 - len(actual_steps)),
             "data_before_retry": before_retry + [[0] * 16 for _ in range(3 - len(before_retry))],
             "service_count": len(services), "services": services + [[0] * 6 for _ in range(3 - len(services))],
             "device_value": 0x42 if scenario == "device" and completed > 2 else 0x12345678}
    return selected, bad, completed, requested, faulted, prefixes, native_faults, actual_steps, steps


def check(cell, expected_scenario=None):
    require(type(cell) is dict, "expected protected32 per-instruction report")
    scenario = cell["scenario"]
    if expected_scenario is not None:
        exact(scenario, expected_scenario, "requested scenario")
    nonce = uint(cell["vm_nonce"], 64, "VM nonce")
    require(nonce > 0, "zero VM incarnation")
    mutation = cell["reply_mutation"]
    recovery = cell["recovery"]
    capture = uint(recovery["capture_pc"], 64, "capture PC")
    selected, bad, completed, requested, faulted, prefixes, faults, fault_steps, steps = expected_progress(scenario, mutation, nonce, capture)
    if completed:
        require(resident_pc(capture), "capture outside resident text")
    else:
        exact(capture, 0, "no native entry before first instruction admission")
    if requested:
        require(resident_pc(uint(cell["service_pc"], 64, "service PC")), "compiler continuation outside resident text")
    else:
        exact(cell["service_pc"], 0, "no compiler call for first-fetch fault")
    current = min(completed, 8)
    # Fetch faults occur before setting operand intent; DS faults occur afterward.
    attempted_memory = current in MEMORY_STEPS and (bad is not None or scenario in ("ds-limit", "ds-readonly"))
    for field, expected in (("case", "fex_moffs_load32_steps"), ("rip", EIP), ("live", 1),
                            ("requests", requested), ("request", steps["requests"][requested - 1] if requested else [0] * 16),
                            ("reply", steps["replies"][requested - 1] if requested else [0] * 16),
                            ("code_before", [0] * 288), ("code_after", code_image(completed)),
                            ("status", 2 if bad else 1), ("detail", 224 if bad else 0),
                            ("current_el", 4), ("tcr", TCR), ("ttbr1", IPA + 0x17000),
                            ("completed", completed), ("stage", 0 if bad else 3 if faulted else 2),
                            ("operand", DATA if attempted_memory else 0), ("width", WIDTHS[current] if attempted_memory else 0)):
        exact(cell[field], expected, field)
    require(uint(cell["sctlr"], 64, "SCTLR") & 0x1005 == 0x1005, "native MMU/cache profile disabled")
    require(uint(cell["actlr"], 64, "ACTLR") & 2 == 2, "TSO profile disabled")
    initial, bank = native_seed()
    exact(cell["initial"], initial, "known native seed")
    if completed == 9:
        entered = copy.deepcopy(prefixes[-2])
        entered["x"][16], entered["x"][30] = CODE + 8 * 32, capture
    else:
        entered = prefixes[-1] if prefixes else initial
    exact(cell["entered"], entered, "last entered or preserved completed frame")
    exact(cell["final_frame"], prefixes[-1] if prefixes else zero_frame(), "full final native prefix")
    exact(cell["steps"], steps, "exact per-instruction progress, source routing and effects")
    for field, expected in (("data_pages_before", data_pages(scenario, 0)), ("data_pages_after", data_pages(scenario, completed)),
                            ("source_pages_before", source_pages(scenario)), ("source_pages_after", source_pages(scenario)),
                            ("bank_before", bank)):
        exact(cell[field], expected, field)
    for field, expected in (("guest_before", guest_seed(scenario)), ("guest_after", guest_after(scenario, completed)),
                            ("control", [0x1000, 0, 1, 0, 0]), ("faults", len(faults)), ("walks", 0),
                            ("retries", 0 if scenario in ("device", "hole") else len(faults)),
                            ("vector", 13 if faulted else 0), ("error", 0), ("linear", 0),
                            ("rip", EIP + GUEST_OFFSETS[completed] if faulted else 0),
                            ("tables_before", [0xA4] * 16384), ("tables_after", [0xA4] * 16384),
                            ("memory_after", data_pages(scenario, completed)[0][:256]), ("bank_after", bank)):
        exact(recovery[field], expected, "recovery." + field)
    traces = recovery["traces"]
    require(type(traces) is list and len(traces) == len(faults), "one native fault for each completed memory instruction")
    for trace, frame, step in zip(traces, faults, fault_steps):
        expected = {"frame": frame, "far": DATA, "elr": CODE + step * 32 + 4,
                    "outcome": 4 if scenario in ("device", "hole") else 1, "error": 0}
        exact({key: value for key, value in trace.items() if key not in ("esr", "spsr")}, expected,
              "precise native memory subaccess before repair/service")
        esr = uint(trace["esr"], 64, "native ESR")
        exact(esr >> 26, 0x25, "same-EL native data abort")
        exact((esr >> 6) & 1, int(step == 2), "native access direction")
        require(esr & 63 in range(4, 8), "each access must fault on its cleared native mapping")
        exact(esr & ((1 << 10) | (1 << 8) | (1 << 7)), 0, "direct valid native FAR")
        spsr = uint(trace["spsr"], 64, "native SPSR")
        exact(spsr & 0x3DF, 0x3C4, "masked EL1t")
        exact(spsr & 0xF0000000, initial["nzcv"], "unchanged native arithmetic flags")
    root = [0] * 512
    root[256] = IPA + 0x18000 + 3
    for suffix in ("before", "after"):
        exact(cell["upper_root_" + suffix], root, "upper root " + suffix)
        exact(cell["upper_links_" + suffix], [IPA + offset + 3 for offset in (0x18000, 0x19000, 0x1A000)], "upper links " + suffix)
        exact(cell["upper_leaf_" + suffix], [0] * 512, "no upper operand mapping " + suffix)
    attrs = 3 | (1 << 10) | (3 << 8) | (3 << 53)
    lower = [0] * 512
    lower[258] = (IPA + 0x72000) | attrs | (1 << 11)     # the host-made source alias is nG since S3a
    exact(cell["lower_leaf_before"], lower, "initial cold data leaf and fixed source alias")
    latest_memory = max((step for step in MEMORY_STEPS if step < requested), default=-1)
    if latest_memory >= 0 and latest_memory < completed and scenario not in ("device", "hole"):
        backing = {"a20-off": 0x71000, "linear-wrap": 0x74000}.get(scenario, 0x70000)
        lower[256] = (IPA + backing) | attrs
    exact(cell["lower_leaf_after"], lower, "current native route or uninstalled serviced/rejected route")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path, nargs="+")
    parser.add_argument("--scenario", choices=SCENARIOS)
    args = parser.parse_args()
    for path in args.reports:
        try:
            check(json.loads(path.read_text()), args.scenario)
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise SystemExit(f"{path}: FAIL: {error}") from error
        print(f"{path}: PASS (finite protected32 per-instruction profile only)")


if __name__ == "__main__":
    main()

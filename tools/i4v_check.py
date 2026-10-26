#!/usr/bin/env python3
"""Independent finite V1 load oracle; no runtime/producer implementation imports.

MOV RAX,[RBX] reads one little-endian qword, preserves every other guest field
and advances RIP by three only on success. The faulting instruction commits
nothing. Exact fixture seeds prevent matching-but-corrupted before/after state
from manufacturing a successful check. This is not full I4-V acceptance.
"""

import argparse
import copy
import json
from pathlib import Path

PRIVATE = 0x0000800040000000
CODE = PRIVATE + 0x30000
DATA = 0x100000
HIGH = 0xFFFF800000000000
SOURCE = DATA + 0x2000
BODY = "c4fcdfc81f2003d5"
TCR = 16 | 1 << 8 | 1 << 10 | 3 << 12 | 16 << 16 | 1 << 24 | 1 << 26 | 3 << 28 | 2 << 30 | 2 << 32
IPA = 0x40000000
READ_ONLY_DATA = (IPA + 0x70000) | 3 | (1 << 10) | (3 << 8) | (3 << 53) | (1 << 7)
CASES = {
    "cold": (1, 1, 2, 1, 0, 0),
    "low_operand": (1, 1, 2, 1, 0, 0),
    "load_np": (0, 1, 2, 0, 14, 0),
    "user_denied": (0, 1, 2, 0, 14, 5),
    "fetch_nx": (0, 0, 1, 0, 14, 17),
    "smep": (0, 0, 1, 0, 14, 17),
    "private_operand": (0, 0, 1, 0, 13, 0),
    "noncanonical": (0, 0, 1, 0, 13, 0),
    "noncanonical_operand": (0, 0, 1, 0, 13, 0),
    "noncanonical_rip": (0, 0, 0, 0, 13, 0),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def uint(value, bits, name):
    require(type(value) is int and 0 <= value < 1 << bits, f"{name}: expected u{bits}")
    return value


def exact(actual, expected, name):
    require(type(actual) is type(expected), f"{name}: wrong type")
    if isinstance(expected, dict):
        require(actual.keys() == expected.keys(), f"{name}: wrong fields")
        for key in expected:
            exact(actual[key], expected[key], f"{name}.{key}")
    elif isinstance(expected, list):
        require(len(actual) == len(expected), f"{name}: wrong length")
        for i, (left, right) in enumerate(zip(actual, expected)):
            exact(left, right, f"{name}[{i}]")
    else:
        require(actual == expected, f"{name}: got {actual!r}, expected {expected!r}")


def operand(scenario):
    if scenario in ("noncanonical", "noncanonical_operand"):
        return 0x0000800000100000
    return PRIVATE if scenario == "private_operand" else DATA if scenario == "low_operand" else HIGH + DATA


def native_seed(scenario):
    frame = {"x": [0x1234000000000000 + i * 0x01010101 for i in range(31)],
             "q": [[(i * 17 + j * 3 + 1) % 256 for j in range(16)] for i in range(32)],
             "sp": PRIVATE + 0x54000, "nzcv": 0xA0000000,
             "fpcr": 0x01000000, "fpsr": 0x08000001}
    frame["x"][5], frame["x"][7] = 2, 1
    frame["x"][6] = operand(scenario)
    frame["x"][8] = HIGH + DATA + 0x1800
    frame["x"][11], frame["x"][28] = DATA + 128, PRIVATE + 0x40000
    return frame


def guest_seed(scenario, rip):
    frame = native_seed(scenario)
    guest = {"gpr": [0xBADC000000000000 + i for i in range(16)],
             "rip": rip, "rflags": 0x202,
             "ymm": [[(i * 19 + j * 7 + 3) % 256 for j in range(32)] for i in range(16)],
             "mxcsr": 0x1F80, "x87": [0x37F, 0, 0xFFFF, 0, 0, 0],
             "x87_regs": [[i * 11 + j + 1 for j in range(10)] + [0] * 6 for i in range(8)],
             "control": [0x80010001, 0xDEAD1000, 0x1000, 0x620, 0xD00, 3, 0xFFFF0FF0, 0x400],
             "segments": [[0x10, 0, 0xFFFFFFFF, 0xC093] for _ in range(8)], "reserved": 0}
    guest["segments"][0] = [8, 0, 0xFFFFFFFF, 0xA09B]
    if scenario == "smep":
        guest["control"][3] |= 1 << 20
    if scenario == "user_denied":
        guest["segments"][0] = [0x1B, 0, 0xFFFFFFFF, 0xA0FB]
        guest["segments"][1:6] = [[0x23, 0, 0xFFFFFFFF, 0xC0F3] for _ in range(5)]
    for gpr, native in ((0, 4), (1, 7), (2, 5), (3, 6), (7, 11)):
        guest["gpr"][gpr] = frame["x"][native]
    guest["gpr"][4] = frame["x"][8]
    guest["ymm"][0][:16] = frame["q"][16]
    guest["ymm"][1][:16] = frame["q"][17]
    return guest


def data_seed():
    data = [(i * 13 + 7) % 256 for i in range(256)]
    data[:8] = bytes.fromhex("aabbccdd00000000")
    return data


def expected_guest(scenario, rip):
    guest = guest_seed(scenario, rip)
    completed, _, _, _, vector, _ = CASES[scenario]
    if completed:
        guest["gpr"][0] = int.from_bytes(bytes(data_seed()[:8]), "little")
        guest["rip"] += 3
    if vector == 14:
        guest["control"][1] = rip if scenario in ("fetch_nx", "smep") else operand(scenario)
    return guest


def guest_tables(scenario, rip, execute=True):
    """Exact four-level fixture and independently selected accessed entries.

    Source fetch precedes the load. Permission-rejected leaves do not acquire A
    in this declared finite walker policy; valid traversed parents do. No case
    writes guest data, so no guest entry may acquire D.
    """
    data_leaf, source_leaf = 0x3800, 0x3810
    entries = {0: 0x2007, 256 * 8: 0x2007, 0x1000: 0x3007, 0x2000: 0x4007,
               data_leaf: DATA | 7, data_leaf + 8: (DATA + 4096) | 7,
               source_leaf: SOURCE | 7}
    if scenario == "load_np":
        entries[data_leaf] = (1 << 64) - 2
    elif scenario == "user_denied":
        entries[data_leaf] = DATA | 3
    elif scenario == "fetch_nx":
        entries[source_leaf] |= 1 << 63

    def encode(words):
        result = [0] * 16384
        for offset, value in words.items():
            result[offset:offset + 8] = value.to_bytes(8, "little")
        return result

    before = encode(entries)
    if scenario == "noncanonical_rip":
        return before, list(before)
    source_root = 256 * 8 if rip >= HIGH else 0
    for offset in (source_root, 0x1000, 0x2000):
        entries[offset] |= 32
    if scenario not in ("fetch_nx", "smep"):
        entries[source_leaf] |= 32
    if execute and CASES[scenario][1]:
        entries[0 if operand(scenario) == DATA else 256 * 8] |= 32
    if execute and CASES[scenario][0]:
        entries[data_leaf] |= 32
    return before, encode(entries)


def entered_frame(scenario, capture_pc):
    frame = native_seed(scenario)
    if CASES[scenario][1]:
        frame["x"][16], frame["x"][30] = CODE, capture_pc
    return frame


def zero_frame():
    return {"x": [0] * 31, "q": [[0] * 16 for _ in range(32)],
            "sp": 0, "nzcv": 0, "fpcr": 0, "fpsr": 0}


def check_service(cell, scenario, rip):
    """Bind one live compile to its VM/request/source and returned body.

    Older eager reports predate these fields. Partial service metadata is never
    accepted, and all original eager assertions below remain in force.
    """
    fields = {"live", "requests", "request", "reply", "vm_nonce", "service_pc",
              "code_before", "code_after", "reply_mutation"}
    if not fields.intersection(cell):
        return False
    require(fields.issubset(cell), "incomplete producer-service metadata")
    live = uint(cell["live"], 1, "live")
    mutation = cell["reply_mutation"]
    require(type(mutation) is int and -1 <= mutation < 16, "invalid reply mutation selector")
    rejected = mutation >= 0
    require(not rejected or (live == 1 and scenario == "cold"), "reply mutation requires live cold profile")
    nonce = uint(cell["vm_nonce"], 64, "vm_nonce")
    require(nonce != 0 if live else nonce == 0, "incorrect VM nonce for producer mode")
    requested = int(bool(live and CASES[scenario][1]))
    exact(cell["requests"], requested, "producer requests")
    service_pc = uint(cell["service_pc"], 64, "service_pc")
    if requested:
        require(PRIVATE <= service_pc < PRIVATE + 0x10000 and service_pc % 4 == 0,
                "service continuation outside resident text")
        request = [0x49345601, 1, nonce, 0, 1, 1, 1, rip, 64, 3, 0x038B48, 0, 0, 1, 0, 0]
        reply = list(request)
        reply[0], reply[11], reply[12] = 0x49345602, 8, 0xD503201FC8DFFCC4
        if rejected:
            reply[mutation] ^= 1
    else:
        exact(service_pc, 0, "no continuation without producer request")
        request, reply = [0] * 16, [0] * 16
    exact(cell["request"], request, "producer request identity and source")
    exact(cell["reply"], reply, "producer reply identity and native body")
    code = list(bytes.fromhex(BODY + "c0035fd6"))
    exact(cell["code_before"], [0] * 12 if live else code, "code before compilation")
    exact(cell["code_after"], code if not live or (requested and not rejected) else [0] * 12,
          "native publication after validated reply only")
    return rejected


def check(cell):
    require(type(cell) is dict, "expected I4-V report object")
    scenario = cell["scenario"]
    require(type(scenario) is str and scenario in CASES, "unsupported V1 load scenario")
    rip = uint(cell["rip"], 64, "rip")
    require(rip in ((0x0000800000102000,) if scenario == "noncanonical_rip" else (SOURCE, HIGH + SOURCE)), "unexpected source RIP")
    rejected = check_service(cell, scenario, rip)
    completed, faults, walks, retries, vector, error = CASES[scenario]
    if rejected:
        completed, faults, walks, retries, vector, error = 0, 0, 1, 0, 0, 0
    for field, expected in (("body_hex", BODY), ("status", 2 if rejected else 1), ("detail", 100 if rejected else 0),
                            ("current_el", 4), ("tcr", TCR), ("ttbr1", IPA + 0x17000),
                            ("completed", completed), ("operand", operand(scenario)),
                            ("width", 8), ("stage", 0 if rejected else 2 if completed else 3)):
        exact(cell[field], expected, field)
    require(uint(cell["sctlr"], 64, "sctlr") & 0x1005 == 0x1005, "MMU/cache profile disabled")
    require(uint(cell["actlr"], 64, "actlr") & 2 == 2, "TSO profile disabled")
    exact(cell["initial"], native_seed(scenario), "initial")
    exact(cell["memory_before"], data_seed(), "memory_before")
    bank = [(i * 37 + 19) % 256 for i in range(512)]
    exact(cell["bank_before"], bank, "bank_before")
    recovery = cell["recovery"]
    require(type(recovery) is dict, "recovery: expected object")
    capture = uint(recovery["capture_pc"], 64, "recovery.capture_pc")
    if faults:
        require(PRIVATE <= capture < PRIVATE + 0x10000 and capture % 4 == 0,
                "capture PC outside resident text")
    else:
        exact(capture, 0, "no native entry for pre-execution exception")
    entered = native_seed(scenario) if rejected else entered_frame(scenario, capture)
    exact(cell["entered"], entered, "entered")
    final = copy.deepcopy(entered) if faults else zero_frame()
    if completed:
        final["x"][4] = 0xDDCCBBAA
    exact(cell["final_frame"], final, "final_frame")
    linear = 0 if not vector else rip if scenario in ("fetch_nx", "smep", "noncanonical_rip") else operand(scenario)
    for field, expected in (("control", [0x1000, 3 if scenario == "user_denied" else 0, 1, 1, int(scenario == "smep")]),
                            ("faults", faults), ("walks", walks), ("retries", retries),
                            ("vector", vector), ("error", error), ("linear", linear),
                            ("rip", rip if vector else 0), ("memory_after", data_seed()),
                            ("bank_after", bank), ("guest_before", guest_seed(scenario, rip)),
                            ("guest_after", guest_seed(scenario, rip) if rejected else expected_guest(scenario, rip))):
        exact(recovery[field], expected, "recovery." + field)
    before, after = guest_tables(scenario, rip, execute=not rejected)
    exact(recovery["tables_before"], before, "tables_before")
    exact(recovery["tables_after"], after, "tables_after")
    traces = recovery["traces"]
    require(type(traces) is list and len(traces) == faults, "incorrect native fault trace count")
    for trace in traces:
        exact(trace["frame"], entered, "trace.frame: fault must precede load commit")
        for field, expected in (("far", operand(scenario)), ("elr", CODE),
                                ("outcome", 1 if completed else 2), ("error", error)):
            exact(trace[field], expected, "trace." + field)
        esr = uint(trace["esr"], 64, "trace.esr")
        exact((esr >> 26) & 63, 0x25, "trace.esr.EC")
        exact((esr >> 6) & 1, 0, "trace.esr.WnR")
        require(esr & 63 in range(4, 8), "expected cold native translation fault")
        spsr = uint(trace["spsr"], 64, "trace.spsr")
        exact(spsr & 15, 4, "trace.spsr.EL1t")
        exact(spsr & 0xF0000000, entered["nzcv"], "trace.spsr.NZCV")
    root = [0] * 512
    root[256] = (IPA + 0x18000) | 3
    leaves = [0] * 512
    for suffix in ("before", "after"):
        exact(cell["upper_root_" + suffix], root, "upper root " + suffix)
        exact(cell["upper_links_" + suffix], [(IPA + 0x18000) | 3, (IPA + 0x19000) | 3, (IPA + 0x1A000) | 3], "upper links " + suffix)
    exact(cell["upper_leaf_before"], leaves, "cold upper leaves")
    if completed and scenario != "low_operand":
        leaves[256] = READ_ONLY_DATA
    exact(cell["upper_leaf_after"], leaves, "upper leaf ownership and read-only rights")
    exact(cell["lower_data_leaf_before"], 0, "cold lower data leaf")
    exact(cell["lower_data_leaf_after"], READ_ONLY_DATA if scenario == "low_operand" else 0, "lower data leaf")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path, nargs="+")
    args = parser.parse_args()
    for path in args.reports:
        try:
            check(json.loads(path.read_text()))
        except (KeyError, TypeError, ValueError) as error:
            raise SystemExit(f"{path}: FAIL: {error}") from error
        print(f"{path}: PASS (finite V1 load profile only)")


if __name__ == "__main__":
    main()

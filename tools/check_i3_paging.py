#!/usr/bin/env python3
"""Independent oracle for twelve finite I3 paging scenarios, not full I3.

Finite answers come from the x86 fixture, controlled seeds(1/2/3), and declared
four-level tables. No runtime walker/handler is imported. The selected walk
ordering sets valid nonleaf A before descent; a leaf receives A/D only after
permission succeeds. Source fetch precedes data access. A read fill remains
read-only until the first write establishes D, forcing two native faults.
The large case stops at a 2 MiB PD leaf; its unused PT is never touched.
Carry/overflow each execute once and check both pre-CFINV fault captures and
the completed inverted-carry representation against all six x86 ADD flags.

The original live FEX body has LDAR at16/guest7 and STR at36/guest13. Both
precede CFINV at40: captured carry is direct. Full canonical state and native
frames are compared with independent inputs, including untouched fields.
This does not cover automated maps, helpers, split effects, FP status or SMP.
"""

import argparse
import copy
import json
from pathlib import Path
import sys

from check_i1 import add_flags, check as check_execution, require, uint
from check_i2 import first_seed

PRIVATE = 0x0000800040000000
CODE = PRIVATE + 0x30000
DATA = 0x100000
SOURCE = DATA + 8192
SOURCE_BYTES = bytes.fromhex("660fefc14801d1488b030f94c2f30f7f07")
# Successful bodies, native faults, walks, retries, guest #PF error, guest offset.
CASES = {
    "cold": (2, 2, 3, 2, None, 17),
    "wp_off": (1, 2, 3, 2, None, 17),
    "store_ro": (0, 2, 3, 1, 3, 13),
    "parent_ro": (0, 2, 3, 1, 3, 13),
    "load_np": (0, 1, 2, 0, 0, 7),
    "user_denied": (0, 1, 2, 0, 5, 7),
    "reserved": (0, 1, 2, 0, 9, 7),
    "fetch_nx": (0, 0, 1, 0, 17, 0),
    "smep": (0, 0, 1, 0, 17, 0),
    "carry": (1, 2, 3, 2, None, 17),
    "overflow": (1, 2, 3, 2, None, 17),
    "large": (1, 2, 3, 2, None, 17),
}
DATA_PTE = 0x3000 + 256 * 8
SOURCE_PTE = DATA_PTE + 16


def exact(actual, expected, name):
    """Compare shape and type too: truncated arrays and bool-as-int must fail."""
    require(type(actual) is type(expected), name + ": wrong type")
    if type(expected) is dict:
        require(actual.keys() == expected.keys(), name + ": wrong fields")
        for key in expected:
            exact(actual[key], expected[key], name + "." + key)
    elif type(expected) is list:
        require(len(actual) == len(expected), name + ": wrong length")
        for i, (a, e) in enumerate(zip(actual, expected)):
            exact(a, e, f"{name}[{i}]")
    else:
        require(actual == expected, f"{name}: got {actual!r}, expected {expected!r}")


def controls(scenario):
    return [0x1000, 3 if scenario == "user_denied" else 0, int(scenario != "wp_off"),
            1, int(scenario == "smep")]


def copy_live(guest, native):
    for gpr, reg in ((0, 4), (1, 7), (2, 5), (3, 6), (7, 11)):
        guest["gpr"][gpr] = native["x"][reg]
    guest["ymm"][0][:16] = native["q"][16]
    guest["ymm"][1][:16] = native["q"][17]


def scenario_seed(scenario):
    """The original seed patterns, with only the declared scalar variation."""
    frame, memory, bank = first_seed()
    if scenario in ("carry", "overflow"):
        frame["x"][7] = (1 << (64 if scenario == "carry" else 63)) - 1
        frame["x"][5] = 1
        memory[:8] = (0xCAFE123456789001 + int(scenario == "overflow")).to_bytes(8, "little")
    return frame, memory, bank


def canonical_flags(scenario):
    frame, _, _ = scenario_seed(scenario)
    _, flags = add_flags(frame["x"][7], frame["x"][5])
    return 0x202 | sum(flags[name] << bit for name, bit in
                       (("CF", 0), ("PF", 2), ("AF", 4), ("ZF", 6), ("SF", 7), ("OF", 11)))


def guest_seed(scenario):
    native, _, _ = scenario_seed(scenario)
    g = {"gpr": [0xBADC000000000000 + i for i in range(16)],
         "rip": SOURCE, "rflags": 0x202,
         "ymm": [[(i * 19 + j * 7 + 3) % 256 for j in range(32)] for i in range(16)],
         "mxcsr": 0x1F80, "x87": [0x37F, 0, 0xFFFF, 0, 0, 0],
         "x87_regs": [[i * 11 + j + 1 for j in range(10)] + [0] * 6 for i in range(8)],
         # PAE plus OSFXSR/OSXMMEXCPT: the admitted SSE instructions require OS support.
         "control": [0x80010001, 0xDEAD1000, 0x1000, 0x620, 0xD00, 3, 0xFFFF0FF0, 0x400],
         "segments": [[0x10, 0, 0xFFFFFFFF, 0xC093] for _ in range(8)], "reserved": 0}
    g["segments"][0] = [8, 0, 0xFFFFFFFF, 0xA09B]
    if scenario == "wp_off":
        g["control"][0] &= ~(1 << 16)
    if scenario == "smep":
        g["control"][3] |= 1 << 20
    if scenario == "user_denied":
        g["segments"][0] = [0x1B, 0, 0xFFFFFFFF, 0xA0FB]
        g["segments"][1:6] = [[0x23, 0, 0xFFFFFFFF, 0xC0F3] for _ in range(5)]
    copy_live(g, native)
    return g


def tables(scenario):
    entries = {0: 0x2007, 0x1000: 0x3007, 0x2000: 0x4007,
               DATA_PTE: DATA | 7, DATA_PTE + 8: (DATA + 4096) | 7,
               SOURCE_PTE: SOURCE | 7}
    if scenario == "load_np":
        entries[DATA_PTE] = (1 << 64) - 2
    elif scenario in ("store_ro", "wp_off"):
        entries[DATA_PTE] = DATA | 5
    elif scenario == "parent_ro":
        entries[0x2000] = 0x4005
    elif scenario == "user_denied":
        entries[DATA_PTE] = DATA | 3
    elif scenario == "reserved":
        entries[DATA_PTE] = DATA | 7 | (1 << 40)
    elif scenario == "fetch_nx":
        entries[SOURCE_PTE] = SOURCE | 7 | (1 << 63)
    elif scenario == "large":
        entries[0x2000] = 0x87  # Present/RW/user/PS: 2 MiB leaf mapping GPA zero.

    def encode(items):
        result = [0] * 16384
        for offset, word in items.items():
            result[offset:offset + 8] = word.to_bytes(8, "little")
        return result

    before = encode(entries)
    for offset in (0, 0x1000, 0x2000):
        entries[offset] |= 32
    if scenario not in ("fetch_nx", "smep", "large"):
        entries[SOURCE_PTE] |= 32
    if scenario in ("cold", "wp_off", "store_ro", "parent_ro", "carry", "overflow"):
        entries[DATA_PTE] |= 32
    if scenario in ("cold", "wp_off", "carry", "overflow"):
        entries[DATA_PTE] |= 64
    if scenario == "large":
        entries[0x2000] |= 64
    return before, encode(entries)


def native_prefix(capture_pc, stage, scenario="cold"):
    """Faults precede CFINV; carry is direct and deferred PF/AF roots are live."""
    frame, memory, _ = scenario_seed(scenario)
    lhs, rhs = frame["x"][7], frame["x"][5]
    result, flags = add_flags(lhs, rhs)
    frame["x"][16], frame["x"][30] = CODE, capture_pc
    frame["x"][7] = frame["x"][26] = result
    frame["x"][27] = lhs ^ rhs
    frame["q"][16] = [a ^ b for a, b in zip(frame["q"][16], frame["q"][17])]
    frame["nzcv"] = flags["SF"] << 31 | flags["ZF"] << 30 | flags["CF"] << 29 | flags["OF"] << 28
    if stage == "store":
        frame["x"][4] = int.from_bytes(memory[:8], "little")
        frame["x"][5] = (rhs & ~255) | flags["ZF"]
        frame["x"][20] = flags["ZF"]
    return frame


def events(scenario, vm):
    completed, faults, _, _, _, _ = CASES[scenario]
    early = faults == 0
    sequence = [10, 18] if early else [10, 11] + ([12] * completed if completed else [18])
    issued = done = pending = cached = 0
    rows, requests = [], []
    padded = SOURCE_BYTES + bytes(7)
    words = [int.from_bytes(padded[i:i + 8], "little") for i in (0, 8, 16)]
    for event in sequence:
        if event == 11:
            issued = pending = 1
        elif event == 12 or (event == 18 and not early):
            pending, cached = 0, 1
            done += int(event == 12)
        packet = [event, 3, pending, 1, 1, vm, 17, *words, 1, 0, SOURCE, issued, done, issued]
        rows.append({"origin": "resident", "event": event, "packet": packet,
                     "pending": pending, "cached": cached, "rejections": 0,
                     "busy": 0, "cancellations": 0})
        if event == 11:
            requests.append(list(packet))
    return rows, requests


def check(cell):
    require(type(cell) is dict and cell.get("case") == "i3", "expected I3 paging object")
    scenario = cell["scenario"]
    require(type(scenario) is str and scenario in CASES, "unsupported I3 paging scenario")
    completed, faults, walks, retries, error, offset = CASES[scenario]
    vm = uint(cell["vm"], 64, "vm")
    require(vm != 0, "zero VM incarnation")
    rows, requests = events(scenario, vm)
    exact(cell["events"], rows, "events")
    exact(cell["requests"], requests, "requests")
    for field, expected in (("final_pending", 0), ("final_executions", completed), ("final_status", 1)):
        exact(cell[field], expected, field)
    p = cell["paging"]
    require(type(p) is dict, "paging: expected object")
    pc = uint(p["capture_pc"], 64, "paging.capture_pc")
    if faults:
        require(PRIVATE <= pc < PRIVATE + 0x10000 and pc % 4 == 0,
                "capture target outside resident text")
    else:
        exact(pc, 0, "no native entry before fetch fault")
    linear = 0 if error is None else SOURCE if offset == 0 else DATA + (128 if offset == 13 else 0)
    for field, expected in (("control", controls(scenario)), ("faults", faults), ("walks", walks),
                            ("retries", retries), ("vector", 0 if error is None else 14),
                            ("error", error or 0), ("linear", linear),
                            ("rip", 0 if error is None else SOURCE + offset)):
        exact(p[field], expected, "paging." + field)
    before, after = tables(scenario)
    exact(p["tables_before"], before, "tables_before")
    exact(p["tables_after"], after, "tables_after")
    exact(p["guest_before"], guest_seed(scenario), "guest_before")
    require(type(p["traces"]) is list and len(p["traces"]) == faults, "wrong trace count")
    for index, trace in enumerate(p["traces"]):
        label = f"traces[{index}]"
        require(type(trace) is dict, label + ": expected object")
        expected = native_prefix(pc, "load" if index == 0 else "store", scenario)
        exact(trace["frame"], expected, label + ".frame")
        for field in ("esr", "far", "elr", "spsr", "outcome", "error"):
            uint(trace[field], 64, label + "." + field)
        exact(trace["far"], DATA + (128 if index else 0), label + ".far")
        exact(trace["elr"], CODE + (36 if index else 16), label + ".elr")
        exact((trace["esr"] >> 26) & 63, 0x25, label + ".ESR.EC")
        exact((trace["esr"] >> 6) & 1, index, label + ".ESR.WnR")
        require(trace["esr"] & 63 in (range(12, 16) if index else range(4, 8)),
                label + ": expected permission/translation syndrome")
        # FnV/S1PTW/EA must not hide a different fault behind a plausible FAR.
        exact(trace["esr"] & ((1 << 10) | (1 << 8) | (1 << 7)), 0, label + ".ESR provenance")
        exact(trace["spsr"] & 0xF0000000, expected["nzcv"], label + ".SPSR.NZCV")
        exact(trace["spsr"] & 0x3DF, 0x3C4, label + ".SPSR DAIF/EL1t")
        terminal = error is not None and index == faults - 1
        exact(trace["outcome"], 2 if terminal else 1, label + ".outcome")
        exact(trace["error"], error if terminal else 0, label + ".error")

    frame, memory, bank = scenario_seed(scenario)
    require(type(cell["executions"]) is list and len(cell["executions"]) == completed,
            "wrong completed execution count")
    for index, execution in enumerate(cell["executions"]):
        label = f"executions[{index}]"
        for field, expected in (("case", "live_load"), ("object_pc", CODE), ("capture_pc", pc),
                                ("memory_base", DATA), ("source_generation", 1), ("context_generation", 1)):
            exact(execution[field], expected, label + "." + field)
        exact(execution["initial"], frame, label + ".initial controlled seed/continuity")
        exact(execution["memory_before"], memory, label + ".memory continuity")
        exact(execution["bank_before"], bank, label + ".bank continuity")
        check_execution(execution)
        frame = copy.deepcopy(execution["captured"])
        memory = list(execution["memory_after"])
    exact(p["memory_after"], memory, "paging.memory_after")
    exact(p["bank_after"], bank, "paging.bank_after")
    guest = guest_seed(scenario)
    if offset:
        final = frame if completed else native_prefix(pc, "store" if offset == 13 else "load", scenario)
        copy_live(guest, final)
        guest["rflags"] = canonical_flags(scenario)
        guest["rip"] = SOURCE + offset
    if error is not None:
        guest["control"][1] = linear
    exact(p["guest_after"], guest, "guest_after")


def synthetic(scenario):
    """Build valid oracle inputs; corruption tests below challenge each boundary."""
    completed, faults, walks, retries, error, offset = CASES[scenario]
    pc = PRIVATE + 0x1000 if faults else 0
    vm = 0x123456789ABCDEF
    rows, requests = events(scenario, vm)
    before, after = tables(scenario)
    frame, memory, bank = scenario_seed(scenario)
    executions = []
    for _ in range(completed):
        captured = copy.deepcopy(frame)
        lhs, rhs = frame["x"][7], frame["x"][5]
        result, flags = add_flags(lhs, rhs)
        captured["x"][4] = int.from_bytes(memory[:8], "little")
        captured["x"][5] = (rhs & ~255) | flags["ZF"]
        captured["x"][20] = flags["ZF"]
        captured["x"][7] = captured["x"][26] = result
        captured["x"][27] = lhs ^ rhs
        captured["x"][16], captured["x"][30] = CODE, pc
        captured["q"][16] = [a ^ b for a, b in zip(frame["q"][16], frame["q"][17])]
        captured["nzcv"] = flags["SF"] << 31 | flags["ZF"] << 30 | (1 - flags["CF"]) << 29 | flags["OF"] << 28
        next_memory = list(memory)
        next_memory[128:144] = captured["q"][16]
        executions.append({"case": "live_load", "status": 1, "initial": frame, "captured": captured,
                           "memory_base": DATA, "memory_before": memory, "memory_after": next_memory,
                           "bank_before": list(bank), "bank_after": list(bank), "object_pc": CODE,
                           "capture_pc": pc, "source_generation": 1, "context_generation": 1})
        frame, memory = copy.deepcopy(captured), next_memory
    traces = []
    for i in range(faults):
        terminal = error is not None and i == faults - 1
        prefix = native_prefix(pc, "store" if i else "load", scenario)
        traces.append({"frame": prefix,
                       "esr": (0x25 << 26) | (1 << 25) | (79 if i else 7),
                       "far": DATA + (128 if i else 0), "elr": CODE + (36 if i else 16),
                       "spsr": 0x3C4 | prefix["nzcv"], "outcome": 2 if terminal else 1, "error": error if terminal else 0})
    guest = guest_seed(scenario)
    linear = 0 if error is None else SOURCE if offset == 0 else DATA + (128 if offset == 13 else 0)
    if offset:
        copy_live(guest, frame if completed else traces[-1]["frame"])
        guest["rflags"], guest["rip"] = canonical_flags(scenario), SOURCE + offset
    if error is not None:
        guest["control"][1] = linear
    return {"case": "i3", "scenario": scenario, "vm": vm, "events": rows, "requests": requests,
            "executions": executions, "final_pending": 0, "final_executions": completed, "final_status": 1,
            "paging": {"capture_pc": pc, "guest_before": guest_seed(scenario), "guest_after": guest,
                       "control": controls(scenario), "faults": faults, "walks": walks, "retries": retries,
                       "vector": 0 if error is None else 14, "error": error or 0, "linear": linear,
                       "rip": 0 if error is None else SOURCE + offset, "traces": traces,
                       "tables_before": before, "tables_after": after,
                       "memory_after": memory, "bank_after": bank}}


def self_test():
    valid = {scenario: synthetic(scenario) for scenario in CASES}
    # Explicit known answers anchor the arithmetic model used by synthetic data.
    for scenario, rflags, before_cfinv, after_cfinv in (
            ("carry", 0x257, 0x60000000, 0x40000000),
            ("overflow", 0xA96, 0x90000000, 0xB0000000)):
        cell = valid[scenario]
        exact(cell["paging"]["guest_after"]["rflags"], rflags, scenario + " known x86 flags")
        for trace in cell["paging"]["traces"]:
            exact(trace["frame"]["nzcv"], before_cfinv, scenario + " known direct carry")
        exact(cell["executions"][0]["captured"]["nzcv"], after_cfinv, scenario + " known final inverted carry")
    for cell in valid.values():
        check(cell)
    mutations = [
        ("missing retry", "cold", ("paging", "retries"), 1),
        ("cached rewalk", "cold", ("paging", "walks"), 4),
        ("internal CR2 change", "cold", ("paging", "guest_after", "control", 1), DATA),
        ("wrong fault address", "store_ro", ("paging", "linear"), DATA),
        ("wrong guest site", "store_ro", ("paging", "guest_after", "rip"), SOURCE + 7),
        ("fault turned successful", "load_np", ("final_executions",), 1),
        ("nonpresent reserved precedence", "load_np", ("paging", "error"), 9),
        ("read erroneously dirty", "store_ro", ("paging", "tables_after", DATA_PTE), 0x65),
        ("denied leaf accessed", "user_denied", ("paging", "tables_after", DATA_PTE), 0x23),
        ("nonleaf accessed lost", "reserved", ("paging", "tables_after", 0), 7),
        ("source accessed on NX", "fetch_nx", ("paging", "tables_after", SOURCE_PTE), 0x27),
        ("unrelated PTE changed", "cold", ("paging", "tables_after", DATA_PTE + 8), 0x27),
        ("uncontrolled input", "cold", ("executions", 0, "initial", "x", 19), 0),
        ("load overwrote destination on fault", "load_np", ("paging", "traces", 0, "frame", "x", 4), 0),
        ("prefix duplicated", "store_ro", ("paging", "traces", 1, "frame", "q", 16, 0), 0x11),
        ("SETZ skipped", "store_ro", ("paging", "guest_after", "gpr", 2), 2),
        ("carry inverted early", "store_ro", ("paging", "traces", 1, "frame", "nzcv"), 0x20000000),
        ("wrong fault site", "load_np", ("paging", "traces", 0, "elr"), CODE + 36),
        ("wrong native fault kind", "load_np", ("paging", "traces", 0, "esr"), 0),
        ("wrong native mode", "load_np", ("paging", "traces", 0, "spsr"), 0),
        ("wrong outcome", "store_ro", ("paging", "traces", 1, "outcome"), 1),
        ("fault performed store", "store_ro", ("paging", "memory_after", 128), 0),
        ("bank clobbered", "cold", ("paging", "bank_after", 511), 0),
        ("fetch computed flags", "smep", ("paging", "guest_after", "rflags"), 0x206),
        ("fetch invoked compiler", "fetch_nx", ("requests",), [[0] * 16]),
        ("wrong request source", "cold", ("requests", 0, 12), DATA),
        ("wrong ABI version", "cold", ("events", 0, "packet", 1), 2),
        ("SSE disabled in canonical profile", "cold", ("paging", "guest_before", "control", 3), 0x20),
        ("extra executions", "cold", ("executions",), []),
        ("truncated table", "cold", ("paging", "tables_after"), []),
        ("truncated native frame", "load_np", ("paging", "traces", 0, "frame", "q"), []),
        ("bool counter", "cold", ("final_status",), True),
        ("carry seed operand", "carry", ("executions", 0, "initial", "x", 7), 1),
        ("overflow seed scalar", "overflow", ("executions", 0, "memory_before", 0), 3),
        ("carry SETZ skipped", "carry", ("paging", "traces", 1, "frame", "x", 5), 0),
        ("overflow false SETZ", "overflow", ("paging", "traces", 1, "frame", "x", 5), 1),
        ("carry lost AF root", "carry", ("paging", "traces", 0, "frame", "x", 27), 3),
        ("overflow lost AF root", "overflow", ("paging", "traces", 0, "frame", "x", 27), 3),
        ("carry duplicate execution", "carry", ("final_executions",), 2),
        ("overflow duplicate walk", "overflow", ("paging", "walks"), 4),
        ("large duplicate cache execution", "large", ("final_executions",), 2),
        ("large missing PS", "large", ("paging", "tables_before", 0x2000), 7),
        ("large missing leaf D", "large", ("paging", "tables_after", 0x2000), 0xA7),
        ("large dirtied nonleaf", "large", ("paging", "tables_after", 0x1000), 0x67),
        ("large touched unused data PTE", "large", ("paging", "tables_after", DATA_PTE), 0x27),
        ("large touched unused source PTE", "large", ("paging", "tables_after", SOURCE_PTE), 0x27),
    ]
    for scenario in ("carry", "overflow"):
        cell = valid[scenario]
        for flag, bit in (("CF", 0), ("PF", 2), ("AF", 4), ("ZF", 6), ("SF", 7), ("OF", 11)):
            mutations.append((scenario + " canonical " + flag, scenario,
                              ("paging", "guest_after", "rflags"),
                              cell["paging"]["guest_after"]["rflags"] ^ (1 << bit)))
        for bit in range(28, 32):
            mutations.append((scenario + " final native flags " + str(bit), scenario,
                              ("executions", 0, "captured", "nzcv"),
                              cell["executions"][0]["captured"]["nzcv"] ^ (1 << bit)))
            for index in (0, 1):
                for field in ("frame", "spsr"):
                    path = ("paging", "traces", index, "frame", "nzcv") if field == "frame" else (
                        "paging", "traces", index, "spsr")
                    initial = cell["paging"]["traces"][index]["frame"]["nzcv"] if field == "frame" else (
                        cell["paging"]["traces"][index]["spsr"])
                    mutations.append((scenario + f" fault{index} {field} flags {bit}", scenario,
                                      path, initial ^ (1 << bit)))
    for field, path in (("YMM high", ("ymm", 0, 31)), ("x87 register", ("x87_regs", 7, 9)),
                        ("x87 tag", ("x87", 2)), ("MXCSR", ("mxcsr",)),
                        ("segment", ("segments", 7, 3)), ("XCR0", ("control", 5)),
                        ("untouched GPR", ("gpr", 15))):
        mutations.append((field, "cold", ("paging", "guest_after", *path), 0))
    for label, scenario, path, value in mutations:
        bad = copy.deepcopy(valid[scenario])
        target = bad
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        try:
            check(bad)
        except ValueError:
            continue
        raise ValueError("failed to reject corruption: " + label)
    print(f"PASS: {len(CASES)} I3 paging scenarios and {len(mutations)} corruption controls (not full I3)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if not args.paths and not args.self_test:
        parser.error("provide JSON scenario files, or --self-test")
    if args.self_test:
        self_test()
    failures = 0
    for path in args.paths:
        try:
            check(json.loads(path.read_text()))
            print(f"PASS: {path}")
        except (OSError, ValueError, KeyError, TypeError) as error:
            failures += 1
            print(f"FAIL: {path}: {error}", file=sys.stderr)
    return int(failures != 0)


if __name__ == "__main__":
    sys.exit(main())

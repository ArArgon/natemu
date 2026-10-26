#!/usr/bin/env python3
"""Independent oracle for the six finite I2 translation-service scenarios.

The reference schedule comes from the I2 behavior contract. It does not import
the Rust transition code or worker. Every resident MISS owns one compiler call;
a host rejection observes the previous resident snapshot without changing it.
Only an admitted reply or a cached dispatch may add an execution. Rejected old
replies preserve the new slot, including its ID and source/context generations.

Per-execution x86 semantics are checked by check_i1. Here, the controlled I1
seed(3), full captured-state continuity, and exact request/event association
prevent independently valid but reset, duplicated, or stale executions passing.
The runner separately links requests to retained live compiler inputs/outputs.
"""

import argparse
import copy
import json
from pathlib import Path

from check_i1 import resident_pc
import sys

from check_i1 import check as check_execution, equal, require, state, uint, values

PRIVATE = 0x0000800040000000
CODE = PRIVATE + 0x30000
DATA = 0x100000
XOR = bytes.fromhex("660fefc14801d1488b030f94c2f30f7f07")
OR = bytes.fromhex("660febc14801d1488b030f94c2f30f7f07")
SCHEDULES = {
    "live": "ready miss cold hit source miss cold context miss cold",
    "source_stale": "ready miss source miss host_stale stale cold",
    "context_stale": "ready miss context miss host_stale stale cold",
    "cancel": "ready miss cancel miss host_stale stale cold",
    "capacity": "ready miss busy cold hit",
    "bad_reply": "ready miss host_stale stale host_bad bad cold",
}
COUNTS = {"live": (3, 4), "source_stale": (2, 1), "context_stale": (2, 1),
          "cancel": (2, 1), "capacity": (1, 2), "bad_reply": (1, 1)}
EVENTS = {"ready": 10, "miss": 11, "cold": 12, "hit": 12, "busy": 13,
          "source": 14, "context": 14, "cancel": 15, "stale": 16,
          "host_stale": 16, "bad": 17, "host_bad": 17}
SIDE_FIELDS = ("pending", "cached", "rejections", "busy", "cancellations")


def reference(scenario, vm):
    """Construct expected finite traces from ownership and effect-count rules."""
    source_generation = context_generation = 1
    issued = completed = pending = cached = rejected = busy = cancelled = 0
    source = XOR
    packet_event = 10
    rows, requests, executions = [], [], []
    for action in SCHEDULES[scenario].split():
        host = action.startswith("host_")
        if action == "miss":
            require(not pending and not cached, "invalid reference miss")
            issued += 1
            pending = issued
        elif action in ("cold", "hit"):
            require(bool(pending) if action == "cold" else bool(cached), "invalid reference execution")
            pending, cached = 0, 1
            completed += 1
            executions.append(("live_load" if source == XOR else "live_or",
                               source_generation, context_generation))
        elif action in ("source", "context"):
            if action == "source":
                source, source_generation = OR, source_generation + 1
            else:
                context_generation += 1
            pending = cached = 0
        elif action == "cancel":
            require(pending != 0, "invalid reference cancellation")
            pending = 0
            cancelled += 1
        elif action == "busy":
            require(pending != 0, "invalid reference capacity check")
            busy += 1
        elif action in ("stale", "bad"):
            require(pending != 0, "rejection must preserve occupied replacement slot")
            rejected += 1
        if not host:
            packet_event = EVENTS[action]
        padded = source + bytes(24 - len(source))
        words = [int.from_bytes(padded[i:i + 8], "little") for i in (0, 8, 16)]
        packet = [packet_event, 3, pending, source_generation, context_generation,
                  vm, 17, *words, 1, 0, DATA + 8192, issued, completed, issued]
        rows.append({"origin": "host" if host else "resident", "event": EVENTS[action],
                     "packet": packet, "pending": pending, "cached": cached,
                     "rejections": rejected, "busy": busy, "cancellations": cancelled})
        if action == "miss":
            requests.append(list(packet))
    require((len(requests), len(executions)) == COUNTS[scenario], "invalid reference totals")
    return rows, requests, executions


def first_seed():
    """Controlled seed(3) fixture contract, independent of guest-writable state."""
    frame = {"x": [0x1234000000000000 + i * 0x01010101 for i in range(31)],
             "q": [[(r * 17 + b * 3 + 1) % 256 for b in range(16)] for r in range(32)],
             "sp": PRIVATE + 0x54000, "nzcv": 0xA0000000,
             "fpcr": 0x01000000, "fpsr": 0x08000001}
    frame["x"][5], frame["x"][7] = 2, 1
    frame["x"][6], frame["x"][11], frame["x"][28] = DATA, DATA + 128, PRIVATE + 0x40000
    memory = [(i * 13 + 7) % 256 for i in range(256)]
    memory[:8] = (0xCAFE123456789003).to_bytes(8, "little")
    bank = [(i * 37 + 19) % 256 for i in range(512)]
    return frame, memory, bank


def check(cell):
    require(type(cell) is dict and cell.get("case") == "i2", "expected I2 scenario object")
    scenario = cell["scenario"]
    require(scenario in SCHEDULES, f"unsupported I2 scenario: {scenario!r}")
    vm = uint(cell["vm"], 64, "vm")
    require(vm != 0, "zero VM incarnation cookie")
    expected_events, expected_requests, expected_executions = reference(scenario, vm)
    for field, count in (("events", len(expected_events)), ("requests", len(expected_requests)),
                         ("executions", len(expected_executions))):
        require(type(cell[field]) is list and len(cell[field]) == count,
                f"{field}: expected exactly {count} entries")
    previous = None
    for index, (actual, expected) in enumerate(zip(cell["events"], expected_events)):
        name = f"events[{index}]"
        require(type(actual) is dict, name + ": expected object")
        values(actual["packet"], 16, 64, name + ".packet")
        for field in ("event", *SIDE_FIELDS):
            uint(actual[field], 64, name + "." + field)
        for field, value in expected.items():
            equal(actual[field], value, name + "." + field)
        if actual["origin"] == "host":
            for field in ("packet", *SIDE_FIELDS):
                equal(actual[field], previous[field], name + ": host mutated " + field)
        previous = actual
    for index, (actual, expected) in enumerate(zip(cell["requests"], expected_requests)):
        values(actual, 16, 64, f"requests[{index}]")
        equal(actual, expected, f"requests[{index}]: must equal corresponding resident MISS")

    frame, memory, bank = first_seed()
    capture_pc = None
    for index, (execution, tags) in enumerate(zip(cell["executions"], expected_executions)):
        name = f"executions[{index}]"
        require(type(execution) is dict, name + ": expected object")
        equal(execution["case"], tags[0], name + ".case/source")
        for field, expected in (("source_generation", tags[1]), ("context_generation", tags[2]),
                                ("memory_base", DATA), ("object_pc", CODE)):
            equal(uint(execution[field], 64, name + "." + field), expected, name + "." + field)
        pc = uint(execution["capture_pc"], 64, name + ".capture_pc")
        require(resident_pc(pc),
                name + ": capture target outside resident text")
        if capture_pc is not None:
            equal(pc, capture_pc, name + ": changed return gateway")
            # Gate-only values are explicit; every other input is the last capture.
            frame["x"][16], frame["x"][30] = CODE, pc
        capture_pc = pc
        state(execution["initial"], name + ".initial")
        for field in ("x", "q", "sp", "nzcv", "fpcr", "fpsr"):
            equal(execution["initial"][field], frame[field], name + ": seed/continuity " + field)
        values(execution["memory_before"], 256, 8, name + ".memory_before")
        values(execution["bank_before"], 512, 8, name + ".bank_before")
        equal(execution["memory_before"], memory, name + ": memory continuity")
        equal(execution["bank_before"], bank, name + ": bank continuity")
        check_execution(execution)
        frame = copy.deepcopy(execution["captured"])
        memory, bank = list(execution["memory_after"]), list(execution["bank_after"])
    for field, expected in (("final_pending", 0), ("final_executions", len(expected_executions)),
                            ("final_status", 1)):
        equal(uint(cell[field], 64, field), expected, field)


def synthetic(scenario):
    """Known scalar answer: first ADD1+2 gives3; later DL=0 keeps every ADD at3."""
    vm = 0x123456789ABCDEF
    events, requests, tags = reference(scenario, vm)
    frame, memory, bank = first_seed()
    executions = []
    for case, source, context in tags:
        captured = copy.deepcopy(frame)
        captured["x"][4] = 0xCAFE123456789003
        captured["x"][5] = captured["x"][20] = 0
        for reg in (7, 26, 27):
            captured["x"][reg] = 3
        captured["x"][16], captured["x"][30] = CODE, PRIVATE + 0xA0080
        captured["nzcv"] = 0x20000000  # SF/ZF/OF/CF0; final native C is inverted.
        captured["q"][16] = [(a | b) if case == "live_or" else (a ^ b)
                              for a, b in zip(frame["q"][16], frame["q"][17])]
        after = list(memory)
        after[128:144] = captured["q"][16]
        executions.append({"case": case, "status": 1, "initial": copy.deepcopy(frame),
                           "captured": captured, "memory_base": DATA, "memory_before": list(memory),
                           "memory_after": after, "bank_before": list(bank), "bank_after": list(bank),
                           "object_pc": CODE, "capture_pc": PRIVATE + 0xA0080,
                           "source_generation": source, "context_generation": context})
        frame, memory = copy.deepcopy(captured), after
    return {"case": "i2", "scenario": scenario, "vm": vm, "events": events,
            "requests": requests, "executions": executions, "final_pending": 0,
            "final_executions": len(executions), "final_status": 1}


def self_test():
    valid = {scenario: synthetic(scenario) for scenario in SCHEDULES}
    for cell in valid.values():
        check(cell)
    mutations = [
        ("stale executed", "source_stale", lambda c: c["events"][5].__setitem__("event", 12)),
        ("changed request ID", "cancel", lambda c: c["events"][3]["packet"].__setitem__(2, 1)),
        ("stale cleared replacement", "context_stale", lambda c: c["events"][5].__setitem__("pending", 0)),
        ("cache reset", "live", lambda c: c["events"][3].__setitem__("cached", 0)),
        ("host changed state", "bad_reply", lambda c: c["events"][2].__setitem__("rejections", 1)),
        ("host changed packet", "bad_reply", lambda c: c["events"][4]["packet"].__setitem__(0, 17)),
        ("duplicate effect", "live", lambda c: c["executions"].append(copy.deepcopy(c["executions"][-1]))),
        ("duplicate count", "capacity", lambda c: c.__setitem__("final_executions", 3)),
        ("extra capacity compilation", "capacity", lambda c: c["requests"].append(list(c["requests"][0]))),
        ("changed compiler request", "cancel", lambda c: c["requests"][1].__setitem__(2, 1)),
        ("stale source executed", "live", lambda c: c["executions"][2].__setitem__("case", "live_load")),
        ("wrong context executed", "live", lambda c: c["executions"][3].__setitem__("context_generation", 1)),
        ("reset input state", "capacity", lambda c: c["executions"][1].__setitem__("initial", copy.deepcopy(c["executions"][0]["initial"]))),
        ("reset memory", "live", lambda c: c["executions"][1].__setitem__("memory_before", list(c["executions"][0]["memory_before"]))),
        ("uncontrolled seed", "cancel", lambda c: c["executions"][0]["initial"]["x"].__setitem__(19, 0)),
        ("wrong VM", "source_stale", lambda c: c["events"][1]["packet"].__setitem__(5, 1)),
        ("source padding", "live", lambda c: c["requests"][0].__setitem__(9, 0x107)),
        ("retained slot on shutdown", "live", lambda c: c.__setitem__("final_pending", 3)),
    ]
    for label, scenario, mutate in mutations:
        bad = copy.deepcopy(valid[scenario])
        mutate(bad)
        try:
            check(bad)
        except ValueError:
            continue
        raise ValueError("failed to reject corruption: " + label)
    print(f"PASS: six I2 scenarios and {len(mutations)} lifecycle/continuity corruption controls")


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

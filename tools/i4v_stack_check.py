#!/usr/bin/env python3
"""Independent oracle for the finite live FEX stack profile, not full I4-V.

Architectural prefixes use the separately authored upstream-case expectation
model. Native scratch/writeback expectations follow the retained generated
instructions; no runtime, host, producer or walker implementation is imported.
"""

import argparse
import copy
import importlib.util
import json
from pathlib import Path

from check_i1 import resident_pc

from i4v_check import CODE, DATA, HIGH, IPA, PRIVATE, SOURCE, TCR, exact, require, uint, zero_frame
from i4v_check import guest_seed as load_guest_seed, native_seed as load_native_seed

_spec = importlib.util.spec_from_file_location(
    "i4v_fixture_expectations", Path(__file__).resolve().parents[1] / "fixtures/i4v/expectations.py")
_expectations = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_expectations)

SOURCE_BYTES = bytes.fromhex("68bbbcbdbe6668525166686261488b42f88b5af4")
BODY = bytes.fromhex("94688892b4d7b7f2148d1ff8542a8a5214ed1f78542c8c5214ed1f78b42000d184fedfc81f2003d5b43000d186fedf881f2003d5")
GUEST_OFFSETS = [0, 5, 9, 13, 17, 20]
NATIVE_OFFSETS = [0, 12, 20, 28, 40, 52]
SITE_OFFSETS = [8, 4, 4, 4, 4]
WIDTHS = [8, 2, 2, 8, 4]
# Committed guest instructions, native fault sites, vector, error.
CASES = {
    "cold": (5, [0, 1], 0, 0),
    "low_stack": (5, [0, 1], 0, 0),
    "ro_first": (0, [0], 14, 3),
    "ro_after_first": (1, [0, 1], 14, 3),
    "np_after_first": (1, [0, 1], 14, 2),
    "wp_off": (5, [0, 1], 0, 0),
    "user_ro": (0, [0], 14, 7),
    "user_denied": (0, [0], 14, 7),
    "user_ok": (5, [0, 1], 0, 0),
    "parent_ro": (0, [0], 14, 3),
    "fetch_nx": (0, [], 14, 17),
    "smep": (0, [], 14, 17),
    "ss_first": (0, [], 12, 0),
    "ss_after_first": (1, [0], 12, 0),
    "ss_span": (0, [], 12, 0),
    "gp_after_pushes": (3, [0, 1], 13, 0),
    "read_np_after_pushes": (3, [0, 1, 3], 14, 0),
}


def stack_top(scenario):
    return {"low_stack": DATA + 0x1008, "ss_first": PRIVATE + 8,
            "ss_after_first": HIGH + 8, "ss_span": HIGH + 4}.get(scenario, HIGH + DATA + 0x1008)


def data_base(scenario):
    return DATA if scenario == "low_stack" else HIGH if scenario == "ss_after_first" else HIGH + DATA


def rdx(scenario):
    return {"gp_after_pushes": PRIVATE + 8,
            "read_np_after_pushes": HIGH + DATA + 0x3008}.get(scenario, stack_top(scenario))


def address(scenario, step):
    return (stack_top(scenario) - (8, 10, 12)[step] if step < 3
            else rdx(scenario) - (8 if step == 3 else 12)) & ((1 << 64) - 1)


def native_seed(scenario):
    frame = load_native_seed("cold")
    frame["x"][5], frame["x"][6], frame["x"][8] = rdx(scenario), DATA, stack_top(scenario)
    return frame


def guest_seed(scenario, rip):
    guest = load_guest_seed("cold", rip)
    guest["gpr"][2], guest["gpr"][3], guest["gpr"][4] = rdx(scenario), DATA, stack_top(scenario)
    if scenario in ("wp_off", "user_ro"):
        guest["control"][0] &= ~(1 << 16)
    if scenario == "smep":
        guest["control"][3] |= 1 << 20
    if scenario in ("user_ro", "user_denied", "user_ok"):
        guest["segments"][0] = [0x1B, 0, 0xFFFFFFFF, 0xA0FB]
        guest["segments"][1:6] = [[0x23, 0, 0xFFFFFFFF, 0xC0F3] for _ in range(5)]
    return guest


def architectural_prefix(scenario, rip, completed):
    guest = guest_seed(scenario, rip)
    base = data_base(scenario)
    memory = dict(enumerate([0xA5] * 8192, base))
    registers = {"RAX": guest["gpr"][0], "RBX": guest["gpr"][3],
                 "RDX": rdx(scenario), "RSP": stack_top(scenario)}
    registers, memory = _expectations.expected_after(
        "fex_push_immediates64", registers, memory, completed)
    guest["gpr"][0], guest["gpr"][3], guest["gpr"][4] = registers["RAX"], registers["RBX"], registers["RSP"]
    guest["rip"] = rip + GUEST_OFFSETS[completed]
    return guest, [memory[base + offset] for offset in range(8192)]


def native_prefixes(scenario, capture_pc):
    """Full successful native frames, including generated scratch x20."""
    frame, result = native_seed(scenario), []
    for step in range(5):
        frame["x"][16], frame["x"][30] = CODE + step * 64, capture_pc
        frame["x"][20] = (0xFFFFFFFFBEBDBCBB, 0x5152, 0x6162)[step] if step < 3 else address(scenario, step)
        if step < 3:
            frame["x"][8] = address(scenario, step)
        elif step == 3:
            frame["x"][4] = 0xFFFFFFFFBEBDBCBB
        else:
            frame["x"][6] = 0x51526162
        result.append(copy.deepcopy(frame))
    return result


def native_entry(scenario, step, capture_pc):
    frame = native_seed(scenario) if step == 0 else native_prefixes(scenario, capture_pc)[step - 1]
    frame["x"][16], frame["x"][30] = CODE + step * 64, capture_pc
    return frame


def native_fault(scenario, step, capture_pc):
    frame = native_entry(scenario, step, capture_pc)
    frame["x"][20] = (0xFFFFFFFFBEBDBCBB, 0x5152, 0x6162)[step] if step < 3 else address(scenario, step)
    return frame


def packets(nonce, rip, transfers, mutation):
    padded = SOURCE_BYTES + bytes(4)
    source_words = [int.from_bytes(padded[i:i + 8], "little") for i in (0, 8, 16)]
    requests, replies = [[0] * 16 for _ in range(3)], [[0] * 16 for _ in range(3)]
    for chunk in range(transfers):
        request = [0x49345611, 1, nonce, 0, 1, 1, 1, rip, 64, 20, *source_words, 1, 52, chunk]
        body_chunk = BODY[chunk * 24:(chunk + 1) * 24]
        padded_body = body_chunk + bytes(24 - len(body_chunk))
        words = [int.from_bytes(padded_body[i:i + 8], "little") for i in (0, 8, 16)]
        reply = [0x49345612, *request[1:9], 52, *words, 1, chunk, len(body_chunk)]
        if mutation >= 0 and mutation // 16 == chunk:
            reply[mutation % 16] ^= 1
        requests[chunk], replies[chunk] = request, reply
    return requests, replies


def code_image():
    code = [0] * 320
    for step, (start, end) in enumerate(zip(NATIVE_OFFSETS, NATIVE_OFFSETS[1:])):
        body = BODY[start:end] + bytes.fromhex("c0035fd6")
        code[step * 64:step * 64 + len(body)] = body
    return code


def guest_tables(scenario, rip, completed, native_fault_steps):
    words = {0: 0x2007, 0x800: 0x2007, 0x1000: 0x3007, 0x2000: 0x4007,
             0x3800: DATA | 7, 0x3808: (DATA + 4096) | 7, 0x3810: SOURCE | 7}
    if scenario == "ro_first":
        words[0x3808] &= ~2
    elif scenario == "ro_after_first":
        words[0x3800] &= ~2
    elif scenario == "np_after_first":
        words[0x3800] = 0
    elif scenario in ("wp_off", "user_ro"):
        words[0x3800] &= ~2
        words[0x3808] &= ~2
    elif scenario == "user_denied":
        words[0x3808] &= ~4
    elif scenario == "parent_ro":
        words[0x2000] &= ~2
    elif scenario == "fetch_nx":
        words[0x3810] |= 1 << 63
    elif scenario == "ss_after_first":
        words[0x3000] = DATA | 7

    def encode(entries):
        result = [0] * 16384
        for offset, value in entries.items():
            result[offset:offset + 8] = value.to_bytes(8, "little")
        return result

    before = encode(words)
    for offset in (0x800 if rip >= HIGH else 0, 0x1000, 0x2000):
        words[offset] |= 32
    if scenario not in ("fetch_nx", "smep"):
        words[0x3810] |= 32
    for step in native_fault_steps:
        linear = address(scenario, step)
        for offset in (0x800 if linear >= HIGH else 0, 0x1000, 0x2000):
            words[offset] |= 32
        if step < completed:
            leaf = 0x3000 + ((linear >> 12) & 511) * 8
            words[leaf] |= 32 | (64 if step < 3 else 0)
    return before, encode(words)


def native_leaves(scenario, completed):
    """Only successful first writes may install writable owned-RAM routes."""
    attrs = 3 | (1 << 10) | (3 << 8) | (3 << 53)
    lower = [0] * 512
    lower[258] = (IPA + 0x72000) | attrs
    upper = [0] * 512
    before = list(lower)
    target = lower if scenario == "low_stack" else upper
    if completed:
        page = (address(scenario, 0) >> 12) & 511
        target[page] = (IPA + 0x70000 + (0 if scenario == "ss_after_first" else 4096)) | attrs
    if completed >= 2:
        target[256] = (IPA + 0x70000) | attrs
    return before, lower, upper


def outcomes(scenario, mutation):
    completed, sites, vector, error = CASES[scenario]
    requested = int(scenario not in ("fetch_nx", "smep", "ss_first", "ss_span"))
    rejected = mutation >= 0
    if rejected:
        completed, sites, vector, error = 0, [], 0, 0
    entries = [int(step < completed or step in sites) for step in range(5)]
    retries = sum(step < completed for step in sites)
    due = min(completed, 4)
    if scenario in ("fetch_nx", "smep"):
        due = None
    return {
        "completed": completed, "sites": sites, "vector": vector, "error": error,
        "requested": requested, "rejected": rejected,
        "transfers": mutation // 16 + 1 if rejected else 3 * requested,
        "entries": entries, "completions": [int(step < completed) for step in range(5)],
        "retries": retries, "due": due,
    }


def check(cell):
    require(type(cell) is dict, "expected stack report")
    exact(cell["case"], "fex_push_immediates64", "case")
    scenario = cell["scenario"]
    require(type(scenario) is str and scenario in CASES, "unknown stack scenario")
    rip = uint(cell["rip"], 64, "rip")
    require(rip in (SOURCE, HIGH + SOURCE), "unrecognized stack source address")
    mutation = cell["reply_mutation"]
    require(type(mutation) is int and -1 <= mutation < 48, "invalid stack reply mutation")
    require(mutation < 0 or scenario == "cold", "reply mutation outside cold profile")
    outcome = outcomes(scenario, mutation)
    completed, sites, vector, error = (outcome[key] for key in ("completed", "sites", "vector", "error"))
    rejected, requested, due = (outcome[key] for key in ("rejected", "requested", "due"))
    for field, expected in (("live", 1), ("body_hex", BODY.hex()), ("requests", requested),
                            ("status", 2 if rejected else 1), ("detail", 115 if rejected else 0),
                            ("current_el", 4), ("tcr", TCR), ("ttbr1", IPA + 0x17000),
                            ("completed", completed), ("stage", 0 if rejected else 2 if completed == 5 else 3),
                            ("operand", 0 if due is None else address(scenario, due)),
                            ("width", 0 if due is None else WIDTHS[due]),
                            ("code_before", [0] * 320),
                            ("code_after", code_image() if requested and not rejected else [0] * 320)):
        exact(cell[field], expected, field)
    require(uint(cell["sctlr"], 64, "sctlr") & 0x1005 == 0x1005, "MMU/cache profile disabled")
    require(uint(cell["actlr"], 64, "actlr") & 2 == 2, "TSO profile disabled")
    nonce = uint(cell["vm_nonce"], 64, "vm_nonce")
    require(nonce != 0, "zero stack VM nonce")
    service_pc = uint(cell["service_pc"], 64, "service_pc")
    if requested:
        require(resident_pc(service_pc),
                "service continuation outside resident text")
    else:
        exact(service_pc, 0, "no compile before source/first-stack fault")
    requests, replies = packets(nonce, rip, outcome["transfers"], mutation)
    exact(cell["request"], requests[0], "first request identity")
    exact(cell["reply"], replies[0], "first delivered reply")
    exact(cell["initial"], native_seed(scenario), "initial")
    recovery = cell["recovery"]
    capture = uint(recovery["capture_pc"], 64, "capture_pc")
    if any(outcome["entries"]):
        require(resident_pc(capture),
                "capture outside resident text")
    else:
        exact(capture, 0, "no native entry")
    prefixes = native_prefixes(scenario, capture)
    entered, final = native_seed(scenario), zero_frame()
    if any(outcome["entries"]):
        last_entry = max(step for step, count in enumerate(outcome["entries"]) if count)
        entered = native_entry(scenario, last_entry, capture)
        if last_entry < completed:
            final = copy.deepcopy(prefixes[last_entry])
            if completed < 5:
                # A due-address guard fails after the completed frame is staged.
                entered = copy.deepcopy(final)
        else:
            final = native_fault(scenario, last_entry, capture)
    exact(cell["entered"], entered, "last staged native frame")
    exact(cell["final_frame"], final, "last captured native frame")
    expected_prefixes = prefixes[:completed] + [zero_frame() for _ in range(5 - completed)]
    exact(cell["stack"], {"transfers": outcome["transfers"], "entries": outcome["entries"],
                          "completions": outcome["completions"], "requests": requests,
                          "replies": replies, "prefixes": expected_prefixes}, "stack")
    guest, memory = architectural_prefix(scenario, rip, completed)
    linear = 0 if not vector else rip if scenario in ("fetch_nx", "smep") else address(scenario, completed)
    if vector == 14:
        guest["control"][1] = linear
    controls = [0x1000, 3 if scenario in ("user_ro", "user_denied", "user_ok") else 0,
                0 if scenario in ("wp_off", "user_ro") else 1, 1, int(scenario == "smep")]
    bank = [(i * 37 + 19) % 256 for i in range(512)]
    exact(cell["memory_before"], [0xA5] * 8192, "initial physical stack")
    exact(cell["memory_after"], memory, "physical stack and guards")
    exact(cell["bank_before"], bank, "bank_before")
    for field, expected in (("guest_before", guest_seed(scenario, rip)), ("guest_after", guest),
                            ("control", controls), ("faults", len(sites)),
                            ("walks", 1 + len(sites)), ("retries", outcome["retries"]),
                            ("vector", vector), ("error", error), ("linear", linear),
                            ("rip", rip + GUEST_OFFSETS[completed] if vector else 0),
                            ("memory_after", memory[:256]), ("bank_after", bank)):
        exact(recovery[field], expected, "recovery." + field)
    before, after = guest_tables(scenario, rip, completed, sites)
    exact(recovery["tables_before"], before, "guest tables before")
    exact(recovery["tables_after"], after, "guest accessed/dirty bits and permissions")
    traces = recovery["traces"]
    require(type(traces) is list and len(traces) == len(sites), "incorrect native trace count")
    for index, (trace, step) in enumerate(zip(traces, sites)):
        label = f"trace[{index}]"
        exact(trace["frame"], native_fault(scenario, step, capture), label + ".precommit frame")
        permitted = step < completed
        for field, expected in (("far", address(scenario, step)),
                                ("elr", CODE + step * 64 + SITE_OFFSETS[step]),
                                ("outcome", 1 if permitted else 2), ("error", 0 if permitted else error)):
            exact(trace[field], expected, label + "." + field)
        esr = uint(trace["esr"], 64, label + ".esr")
        exact(esr >> 26, 0x25, label + ".data abort")
        exact((esr >> 6) & 1, int(step < 3), label + ".write/read direction")
        require(esr & 63 in range(4, 8), label + ": expected cold translation fault")
        spsr = uint(trace["spsr"], 64, label + ".spsr")
        exact(spsr & 15, 4, label + ".EL1t")
        exact(spsr & 0xF0000000, 0xA0000000, label + ".unchanged flags")
    root = [0] * 512
    root[256] = (IPA + 0x18000) | 3
    for suffix in ("before", "after"):
        exact(cell["upper_root_" + suffix], root, "upper root " + suffix)
        exact(cell["upper_links_" + suffix], [(IPA + p) | 3 for p in (0x18000, 0x19000, 0x1A000)], "upper links " + suffix)
    lower_before, lower_after, upper_after = native_leaves(scenario, completed)
    exact(cell["lower_leaf_before"], lower_before, "lower leaves before")
    exact(cell["lower_leaf_after"], lower_after, "lower routes after")
    exact(cell["upper_leaf_before"], [0] * 512, "upper leaves before")
    exact(cell["upper_leaf_after"], upper_after, "upper routes after")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path, nargs="+")
    for path in parser.parse_args().reports:
        try:
            check(json.loads(path.read_text()))
        except (KeyError, TypeError, ValueError) as error:
            raise SystemExit(f"{path}: FAIL: {error}") from error
        print(f"{path}: PASS (finite V1 stack profile only)")


if __name__ == "__main__":
    main()

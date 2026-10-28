#!/usr/bin/env python3
"""Independent oracle for the finite live paging-off protected32 load profile.

Architectural effects come from the retained upstream-case expectation model.
Native scratch roots are derived from the exact admitted FEX body, separately
from guest semantics. This profile does not claim arbitrary recovery boundaries,
mode transitions or concurrent paging-off execution.
"""

import argparse
import copy
import importlib.util
import json
from pathlib import Path

from check_i1 import resident_pc
from check_i2 import first_seed
from i4v_check import CODE, DATA, IPA, PRIVATE, SOURCE, TCR, exact, require, uint, zero_frame

_spec = importlib.util.spec_from_file_location(
    "i4v_off_fixture_expectations", Path(__file__).resolve().parents[1] / "fixtures/i4v/expectations.py")
_expectations = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_expectations)

SOURCE_BYTES = bytes.fromhex("ba00001000b8420000008902b8ffffffffa10000100089c2b8ffffffff66a10000100089c1")
BODY = bytes.fromhex("0502a05244088052f40305aa1f2003d5a4fc9f880400801295fedf881f2003d5f60304aae40315aae50304aae40316aa94fedf481f2003d5843e40b3e70304aa")
RETURN = bytes.fromhex("c0035fd6")
GUEST_OFFSETS = (0, 5, 10, 12, 17, 22, 24, 29, 35, 37)


def architectural_effects(registers, memory, completed=9):
    """Independent x86 model, including the 16-bit destination merge."""
    return _expectations.expected_after("fex_moffs_load32", registers, memory, completed)


def native_effects(initial, capture, completed):
    """Exact native roots at the first store or after the complete emitted body.

    No one-to-one guest/native boundary is invented: FEX retains the dword load
    in x21 and the temporary all-ones accumulator in x22 before the AX merge.
    """
    frame = copy.deepcopy(initial)
    frame["x"][16], frame["x"][30] = CODE, capture
    frame["x"][4], frame["x"][5], frame["x"][20] = 0x42, DATA, DATA
    if completed:
        frame["x"][4] = frame["x"][7] = 0xFFFF0042
        frame["x"][5] = frame["x"][20] = frame["x"][21] = 0x42
        frame["x"][22] = 0xFFFFFFFF
    return frame


def request_packet(vm, rip, chunk):
    padded = SOURCE_BYTES + bytes(40 - len(SOURCE_BYTES))
    words = [int.from_bytes(padded[index:index + 8], "little") for index in range(0, 40, 8)]
    return [0x49345621, 1, vm, 0, 1, 1, 1, rip, 32, *words, 1, chunk]


def reply_packet(vm, rip, chunk):
    reply = request_packet(vm, rip, chunk)
    part = BODY[chunk * 24:(chunk + 1) * 24]
    padded = part + bytes(24 - len(part))
    reply[0] = 0x49345622
    reply[9:12] = [int.from_bytes(padded[index:index + 8], "little") for index in (0, 8, 16)]
    reply[12], reply[13], reply[15] = chunk, len(part), 64
    return reply


def guest_seed(initial, rip):
    from check_i3_paging import guest_seed as canonical_seed, copy_live
    guest = canonical_seed("cold")
    copy_live(guest, initial)
    for guest_reg, native_reg in ((0, 4), (3, 6), (1, 7), (2, 5), (6, 10), (7, 11), (5, 9), (4, 8)):
        guest["gpr"][guest_reg] = initial["x"][native_reg] & 0xFFFFFFFF
    guest["rip"] = rip
    guest["control"][0], guest["control"][3], guest["control"][4] = 0x10001, 0x600, 0
    guest["segments"] = [[0x10, 0, 0xFFFFFFFF, 0xC093] for _ in range(8)]
    guest["segments"][0] = [8, 0, 0xFFFFFFFF, 0xC09B]
    return guest


def guest_effects(initial, rip, completed):
    guest = guest_seed(initial, rip)
    registers = {name: guest["gpr"][index] for name, index in (("RAX", 0), ("RCX", 1), ("RDX", 2))}
    memory = dict(enumerate([0xA5] * 4096, DATA))
    registers, memory = architectural_effects(registers, memory, completed)
    for name, index in (("RAX", 0), ("RCX", 1), ("RDX", 2)):
        guest["gpr"][index] = registers[name]
    guest["rip"] = rip + GUEST_OFFSETS[completed]
    return guest, [memory[DATA + index] for index in range(4096)]


def native_seed():
    frame, _, bank = first_seed()
    for index in (4, 6, 7, 5, 10, 11, 9, 8):
        frame["x"][index] &= 0xFFFFFFFF
    return frame, bank


def zeros(value):
    if isinstance(value, dict):
        return {key: zeros(item) for key, item in value.items()}
    if isinstance(value, list):
        return [zeros(item) for item in value]
    return 0


def native_tables(scenario, completed):
    attrs = 3 | (1 << 10) | (3 << 8) | (3 << 53)
    lower = [0] * 512
    lower[258] = (IPA + 0x72000) | attrs | (1 << 11)     # the host-made source alias is nG since S3a
    if scenario != "cold":
        lower[256] = (IPA + 0x70000) | attrs | (128 if scenario == "readonly" else 0)
    before = list(lower)
    if completed:
        lower[256] = (IPA + 0x70000) | attrs
    return before, lower


def check(cell, expected_scenario=None, expected_rip=None):
    require(type(cell) is dict, "expected paging-off32 report")
    scenario = cell["scenario"]
    require(scenario in ("cold", "readonly", "warm"), "unsupported paging-off scenario")
    rip = uint(cell["rip"], 64, "rip")
    require(rip in (SOURCE, SOURCE + 0x80), "unsupported protected32 source window")
    if expected_scenario is not None:
        exact(scenario, expected_scenario, "requested scenario")
    if expected_rip is not None:
        exact(rip, expected_rip, "requested source RIP")
    mutation = cell["reply_mutation"]
    require(type(mutation) is int and -1 <= mutation < 48, "invalid reply mutation")
    rejected = mutation >= 0
    nonce = uint(cell["vm_nonce"], 64, "VM nonce")
    require(nonce > 0, "zero VM nonce")
    require(resident_pc(uint(cell["service_pc"], 64, "service PC")), "service outside resident text")
    count = mutation // 16 + 1 if rejected else 3
    requests = [request_packet(nonce, rip, chunk) for chunk in range(count)]
    replies = [reply_packet(nonce, rip, chunk) for chunk in range(count)]
    if rejected:
        replies[-1][mutation % 16] ^= 1
    for field, expected in (("case", "fex_moffs_load32"), ("live", 1), ("requests", 1),
                            ("request", requests[-1]), ("reply", replies[-1]),
                            ("body_hex", BODY.hex()), ("code_before", [0] * 68),
                            ("code_after", [0] * 68 if rejected else list(BODY + RETURN)),
                            ("status", 2 if rejected else 1), ("detail", 204 if rejected else 0),
                            ("current_el", 4), ("tcr", TCR), ("ttbr1", IPA + 0x17000),
                            ("completed", 0 if rejected else 9), ("stage", 0 if rejected else 2),
                            ("operand", 0), ("width", 0)):
        exact(cell[field], expected, field)
    require(uint(cell["sctlr"], 64, "SCTLR") & 0x1005 == 0x1005, "native MMU/cache profile disabled")
    require(uint(cell["actlr"], 64, "ACTLR") & 2 == 2, "TSO profile disabled")
    initial, bank = native_seed()
    exact(cell["initial"], initial, "independent native seed")
    recovery = cell["recovery"]
    capture = uint(recovery["capture_pc"], 64, "capture PC")
    if rejected:
        exact(capture, 0, "rejected reply cannot enter native code")
    else:
        require(resident_pc(capture), "capture outside resident text")
    entered = copy.deepcopy(initial)
    if not rejected:
        entered["x"][16], entered["x"][30] = CODE, capture
    exact(cell["entered"], entered, "native entry")
    exact(cell["final_frame"], zero_frame() if rejected else native_effects(initial, capture, True),
          "complete native effects and preserved scratch/vector/flags state")
    before = guest_seed(initial, rip)
    after, memory = guest_effects(initial, rip, 0 if rejected else 9)
    exact(cell["memory_before"], [0xA5] * 4096, "entire physical data page before")
    exact(cell["memory_after"], memory, "exact physical writes and full-page guard bytes")
    exact(cell["data_page_before"], [0xA5] * 4096, "complete data backing before")
    exact(cell["data_page_after"], memory, "complete data backing after")
    exact(cell["bank_before"], bank, "private bank seed")
    faults = int(not rejected and scenario != "warm")
    for field, expected in (("guest_before", before), ("guest_after", after),
                            ("control", [0x1000, 0, 1, 0, 0]), ("faults", faults),
                            ("walks", 0), ("retries", faults), ("vector", 0), ("error", 0),
                            ("linear", 0), ("rip", 0), ("tables_before", [0xA4] * 16384),
                            ("tables_after", [0xA4] * 16384), ("memory_after", memory[:256]),
                            ("bank_after", bank)):
        exact(recovery[field], expected, "recovery." + field)
    prefix = guest_effects(initial, rip, 2)[0] if faults else zeros(before)
    off = {"a20": 1, "scenario": ("cold", "readonly", "warm").index(scenario),
           "transfers": count, "fetch_gpa": rip, "repair_gpa": DATA if faults else 0,
           "data_before_retry": [0xA5] * 16 if faults else [0] * 16,
           "requests": requests + [[0] * 16 for _ in range(3 - count)],
           "replies": replies + [[0] * 16 for _ in range(3 - count)], "prefix": prefix}
    exact(cell["off"], off, "paging-off physical routing, precise prefix and complete compile identity")
    traces = recovery["traces"]
    require(type(traces) is list and len(traces) == faults, "one actual first-store fault or warm execution")
    for trace in traces:
        expected = {"frame": native_effects(initial, capture, False), "far": DATA,
                    "elr": CODE + 16, "outcome": 1, "error": 0}
        exact({key: value for key, value in trace.items() if key not in ("esr", "spsr")}, expected,
              "first-store precommit fault and identity repair")
        esr = uint(trace["esr"], 64, "fault ESR")
        exact(esr >> 26, 0x25, "same-EL native data abort")
        exact((esr >> 6) & 1, 1, "native write fault")
        require(esr & 63 in (range(4, 8) if scenario == "cold" else range(12, 16)),
                "translation versus native read-only fault")
        exact(esr & ((1 << 10) | (1 << 8) | (1 << 7)), 0, "valid direct native FAR")
        spsr = uint(trace["spsr"], 64, "fault SPSR")
        exact(spsr & 0x3DF, 0x3C4, "masked EL1t at first store")
        exact(spsr & 0xF0000000, initial["nzcv"], "arithmetic flags preserved")
    root = [0] * 512
    root[256] = IPA + 0x18000 + 3
    for suffix in ("before", "after"):
        exact(cell["upper_root_" + suffix], root, "upper root " + suffix)
        exact(cell["upper_links_" + suffix], [IPA + offset + 3 for offset in (0x18000, 0x19000, 0x1A000)],
              "upper links " + suffix)
        exact(cell["upper_leaf_" + suffix], [0] * 512, "no upper guest alias " + suffix)
    lower_before, lower_after = native_tables(scenario, not rejected)
    exact(cell["lower_leaf_before"], lower_before, "initial native data/source permissions")
    exact(cell["lower_leaf_after"], lower_after, "identity data repair with no unrelated mapping")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path, nargs="+")
    parser.add_argument("--scenario", choices=("cold", "readonly", "warm"))
    parser.add_argument("--rip", type=lambda value: int(value, 0))
    args = parser.parse_args()
    for path in args.reports:
        try:
            check(json.loads(path.read_text()), args.scenario, args.rip)
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise SystemExit(f"{path}: FAIL: {error}") from error
        print(f"{path}: PASS (finite paging-off protected32 profile only)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Independent semantic oracle for finite I1/I2 host JSON cells; executes no guest.

The x86 fixture is PXOR xmm0,xmm1; ADD rcx,rdx; MOV rax,[rbx];
SETZ dl; MOVDQU [rdi],xmm0. Expected guest effects use integer/byte arithmetic.
I2's live_or substitutes POR xmm0,xmm1; it has the same scalar/flags effects.
FEX pin 48d71752e2a363d5535181f803b0b0e9666c1825 binds RAX/RCX/RDX/RBX/RDI
to x4/x7/x5/x6/x11 and XMM0/1 to q16/17. Its unchanged 44-byte body also
leaves the ADD result in x26, the original operands' XOR in x27, and SETZ
in x20. These producer roots are checked separately from the guest flags.

Completed-body NZCV holds SF,ZF,!CF,OF. At native load offset16 (guest7)
and store offset36 (guest13), CFINV at40 has NOT happened: carry is direct.
The current fault case is a separate UDF control, not recovery at those sites.
PF is even low-byte parity; AF is carry from the low nibble. We compare those
independent definitions with the captured FEX deferred-root reconstruction.
Source/bindings: experiments/track03/codegen-checkpoint/fixtures.json and
experiments/track03/codegen-comparison/README.md, "Captive leaf extraction".
The fresh i2-producer-probe-20261025-01 run verifies POR changes only the first
native instruction (EOR to ORR), retaining the same producer roots and sites.
"""

import argparse
import copy
import json
from pathlib import Path
import sys

MASK64 = (1 << 64) - 1
NZCV_MASK = 0xF0000000


def require(condition, message):
    if not condition:
        raise ValueError(message)


def uint(value, bits, name):
    require(type(value) is int and 0 <= value < 1 << bits, f"{name}: expected u{bits}")
    return value


def values(value, length, bits, name):
    require(type(value) is list and len(value) == length, f"{name}: expected {length} entries")
    for i, item in enumerate(value):
        uint(item, bits, f"{name}[{i}]")
    return value


def state(value, name):
    require(type(value) is dict, f"{name}: expected state object")
    values(value["x"], 31, 64, name + ".x")
    require(type(value["q"]) is list and len(value["q"]) == 32, f"{name}.q: expected 32 vectors")
    for i, vector in enumerate(value["q"]):
        values(vector, 16, 8, f"{name}.q[{i}]")
    for field in ("sp", "nzcv", "fpcr", "fpsr"):
        uint(value[field], 64, name + "." + field)
    require(value["nzcv"] & ~NZCV_MASK == 0, f"{name}.nzcv: reserved bits set")
    return value


def equal(actual, expected, name):
    if isinstance(expected, list):
        for i, (a, e) in enumerate(zip(actual, expected)):
            equal(a, e, f"{name}[{i}]")
    else:
        require(actual == expected, f"{name}: got {actual!r}, expected {expected!r}")


def add_flags(lhs, rhs):
    """x86 ADD flags using unbounded arithmetic, signed range and nibble carry."""
    total = lhs + rhs
    result = total & MASK64
    signed = lambda x: x if x < 1 << 63 else x - (1 << 64)
    signed_total = signed(lhs) + signed(rhs)
    return result, {
        "CF": int(total > MASK64), "ZF": int(result == 0),
        "SF": result >> 63, "OF": int(not -(1 << 63) <= signed_total < 1 << 63),
        "PF": int(sum((result >> bit) & 1 for bit in range(8)) % 2 == 0),
        "AF": int((lhs & 15) + (rhs & 15) > 15),
    }


def check(cell):
    require(type(cell) is dict, "expected one JSON object per file")
    case = cell["case"]
    statuses = {"control": 1, "live_load": 1, "live_or": 1, "fault": 3, "reject": 2}
    require(case in statuses, f"unsupported case: {case!r}")
    equal(uint(cell["status"], 32, "status"), statuses[case], "status")
    before = values(cell["memory_before"], 256, 8, "memory_before")
    after = values(cell["memory_after"], 256, 8, "memory_after")
    bank_before = values(cell["bank_before"], 512, 8, "bank_before")
    bank_after = values(cell["bank_after"], 512, 8, "bank_after")
    equal(bank_after, bank_before, "bank_after")
    expected_memory = list(before)
    if case == "reject":
        equal(after, before, "memory_after")
        return

    initial = state(cell["initial"], "initial")
    captured = state(cell["captured"], "captured")
    expected = copy.deepcopy(initial)
    for field in ("object_pc", "capture_pc"):
        uint(cell[field], 64, field)
        require(cell[field] % 4 == 0, field + ": unaligned instruction address")
    expected["x"][16] = cell["object_pc"]
    expected["x"][30] = cell["capture_pc"]

    if case in ("live_load", "live_or"):
        base = uint(cell["memory_base"], 64, "memory_base")
        require(base <= (1 << 64) - 256, "memory range wraps")
        load_address, store_address = initial["x"][6], initial["x"][11]
        load, store = load_address - base, store_address - base
        require(0 <= load <= 248 and load_address % 8 == 0, "invalid scalar memory span")
        require(0 <= store <= 240 and store_address % 16 == 0, "invalid vector memory span")
        require(load + 8 <= store or store + 16 <= load, "overlapping operand regions outside I1 profile")
        lhs, rhs = initial["x"][7], initial["x"][5]
        result, flags = add_flags(lhs, rhs)
        vector = [(a | b) if case == "live_or" else (a ^ b)
                  for a, b in zip(initial["q"][16], initial["q"][17])]
        expected["x"][4] = int.from_bytes(bytes(before[load:load + 8]), "little")
        expected["x"][7] = result
        expected["x"][5] = (rhs & ~255) | flags["ZF"]
        expected["x"][20] = flags["ZF"]
        expected["x"][26] = result
        expected["x"][27] = lhs ^ rhs
        expected["q"][16] = vector
        expected_memory[store:store + 16] = vector
        expected["nzcv"] = (flags["SF"] << 31 | flags["ZF"] << 30 |
                            (1 - flags["CF"]) << 29 | flags["OF"] << 28)
        native = captured["nzcv"]
        reconstructed = {
            "CF": 1 - ((native >> 29) & 1), "ZF": (native >> 30) & 1,
            "SF": (native >> 31) & 1, "OF": (native >> 28) & 1,
            "PF": int(bin(captured["x"][26] & 255).count("1") % 2 == 0),
            "AF": ((captured["x"][27] ^ captured["x"][26]) >> 4) & 1,
        }
        for flag, value in flags.items():
            equal(reconstructed[flag], value, "guest flag " + flag)

    if case == "fault":
        fault = cell["fault"]
        for field in ("esr", "far", "elr", "spsr"):
            uint(fault[field], 64, "fault." + field)
        equal((fault["esr"] >> 26) & 63, 0, "fault.ESR.EC (undefined instruction)")
        equal(fault["elr"], cell["object_pc"], "fault.elr")
        equal(fault["spsr"] & NZCV_MASK, initial["nzcv"], "fault.SPSR.NZCV")
        equal(fault["spsr"] & 0x3DF, 0x3C4, "fault.SPSR masked DAIF and EL1t")
        # FAR is not architecturally meaningful for UDF and is deliberately unchecked.

    for field in ("x", "q", "sp", "nzcv", "fpcr", "fpsr"):
        equal(captured[field], expected[field], "captured." + field)
    equal(after, expected_memory, "memory_after")


def self_test():
    initial = {
        "x": [0x1020304050607000 + i for i in range(31)],
        "q": [[(r * 17 + b * 13) % 256 for b in range(16)] for r in range(32)],
        "sp": 0x90000, "nzcv": 0xA0000000, "fpcr": 0x01000000, "fpsr": 0x08000001,
    }
    cell = {"case": "control", "status": 1, "initial": initial,
            "captured": copy.deepcopy(initial), "memory_base": 0x100000,
            "object_pc": 0x200000, "capture_pc": 0x300000,
            "memory_before": list(range(256)), "memory_after": list(range(256)),
            "bank_before": [i % 256 for i in range(512)], "bank_after": [i % 256 for i in range(512)]}
    cell["captured"]["x"][16] = cell["object_pc"]
    cell["captured"]["x"][30] = cell["capture_pc"]
    check(cell)
    fault = copy.deepcopy(cell)
    fault.update(case="fault", status=3, fault={"esr": 1 << 25, "far": 99,
                 "elr": cell["object_pc"], "spsr": initial["nzcv"] | 0x3C4})
    check(fault)
    rejected = {k: v for k, v in cell.items() if k not in ("initial", "captured")}
    rejected.update(case="reject", status=2)
    check(rejected)

    live = copy.deepcopy(cell)
    live["case"] = "live_load"
    for s in (live["initial"], live["captured"]):
        s["x"][6], s["x"][11] = 0x100000, 0x100040
        s["x"][7], s["x"][5] = MASK64, 1
    # Hand-computed MAX+1: result0, CF/ZF/PF/AF1, SF/OF0; completed native C0.
    out = live["captured"]
    out["x"][4] = 0x0706050403020100
    out["x"][7] = out["x"][26] = 0
    out["x"][5] = out["x"][20] = 1
    out["x"][27] = MASK64 - 1
    out["nzcv"] = 0x40000000
    out["q"][16] = [a ^ b for a, b in zip(initial["q"][16], initial["q"][17])]
    live["memory_after"][64:80] = out["q"][16]
    check(live)
    live_or = copy.deepcopy(live)
    live_or["case"] = "live_or"
    live_or["initial"]["q"][16] = [0xAA] * 16
    for s in (live_or["initial"], live_or["captured"]):
        s["q"][17] = [0x0F] * 16
    # Known answer in every lane: AA OR 0F = AF; AA XOR 0F would be A5.
    live_or["captured"]["q"][16] = [0xAF] * 16
    live_or["memory_after"][64:80] = [0xAF] * 16
    check(live_or)
    for lhs, rhs, result, flags in (
        (0, 0, 0, (0, 1, 0, 0, 1, 0)),
        (0x7FFFFFFFFFFFFFFF, 1, 0x8000000000000000, (0, 0, 1, 1, 1, 1)),
        (0x8000000000000000, 0x8000000000000000, 0, (1, 1, 0, 1, 1, 0)),
        (1, 1, 2, (0, 0, 0, 0, 0, 0)),
    ):
        got, f = add_flags(lhs, rhs)
        require((got, tuple(f[k] for k in ("CF", "ZF", "SF", "OF", "PF", "AF"))) ==
                (result, flags), "ADD flag known-answer test")

    mutations = [
        ("preserved GPR", cell, lambda c: c["captured"]["x"].__setitem__(19, 0)),
        ("vector low half", cell, lambda c: c["captured"]["q"][3].__setitem__(0, 0)),
        ("vector upper half", live, lambda c: c["captured"]["q"][17].__setitem__(15, 0)),
        ("native flags", live, lambda c: c["captured"].__setitem__("nzcv", 0x60000000)),
        ("deferred flags", live, lambda c: c["captured"]["x"].__setitem__(27, 0)),
        ("vector store", live, lambda c: c["memory_after"].__setitem__(64, 0)),
        ("out-of-span store", live, lambda c: c["memory_after"].__setitem__(255, 0)),
        ("bank", live, lambda c: c["bank_after"].__setitem__(511, 0)),
        ("SP", cell, lambda c: c["captured"].__setitem__("sp", 0)),
        ("FPCR", live, lambda c: c["captured"].__setitem__("fpcr", 0)),
        ("FPSR", live, lambda c: c["captured"].__setitem__("fpsr", 0)),
        ("fault PC", fault, lambda c: c["fault"].__setitem__("elr", 0)),
        ("fault mode", fault, lambda c: c["fault"].__setitem__("spsr", 0)),
        ("reject side effect", rejected, lambda c: c["memory_after"].__setitem__(0, 1)),
        ("panic", cell, lambda c: c.__setitem__("status", 4)),
        ("OR lower lane", live_or, lambda c: c["captured"]["q"][16].__setitem__(0, 0xA5)),
        ("OR upper lane", live_or, lambda c: c["captured"]["q"][16].__setitem__(15, 0xA5)),
        ("OR store", live_or, lambda c: c["memory_after"].__setitem__(79, 0xA5)),
        ("XOR mislabeled OR", live, lambda c: c.__setitem__("case", "live_or")),
    ]
    for name, original, mutate in mutations:
        bad = copy.deepcopy(original)
        mutate(bad)
        try:
            check(bad)
        except ValueError:
            continue
        raise ValueError("failed to reject corruption: " + name)
    print(f"PASS: five valid cases, four ADD known answers, {len(mutations)} corruption controls")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if not args.paths and not args.self_test:
        parser.error("provide one or more JSON cell files, or --self-test")
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

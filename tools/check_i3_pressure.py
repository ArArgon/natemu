#!/usr/bin/env python3
"""Independent finite I3 allocated-root and normal ADDPS oracle.

Expected guest results come from the x86 instructions and controlled seed, not
runtime materialization. A small integer model of the admitted ARM instructions
checks every captured register and initialized spill byte. Exported root locations
must then resolve to independently expected architectural values at each site.
The helper variant outlines SETZ/DL into a preserve-all resident call, recording
before/staged frames and a deliberate clobber witness. Only cold/load_np/store_ro
and normal RN ADDPS are covered; this is not full I3.
"""
import argparse
import copy
import json
from pathlib import Path

from check_i1 import resident_pc
import struct
import sys

from check_i1 import add_flags, require, state, uint, values
from check_i2 import first_seed
from check_i3_paging import CODE, DATA, PRIVATE, SOURCE, exact, guest_seed, tables

ALIAS = bytes.fromhex("660fefc14801d1488b000f94c2f30f7f07")
FP_ALIAS = bytes.fromhex("0f58c14801d1488b000f94c2f30f7f07")
ENTRY = (4, 6, 7, 5, 10, 11, 9, 8, 12, 13, 14, 15, 16, 17, 19, 29)
ORDER = (0, 3, 1, 2, 6, 7, 5, 4, 8, 9, 10, 11, 12, 13, 14, 15)
ROLES = ["RAX", "RBX", "RCX", "RDX", "RSI", "RDI", "RBP", "RSP"] + [
    f"R{i}" for i in range(8, 16)] + [f"XMM{i}.low" for i in range(16)] + ["pf_raw", "af_raw", "nzcv"]
STACK = PRIVATE + 0x54000
FIXTURES = Path(__file__).with_name("i3_producer") / "testdata"
CASES = {"cold": (1, 2, 3, 2, None), "load_np": (0, 1, 2, 0, 0),
         "store_ro": (0, 2, 3, 1, 3)}


def seed(fp):
    f, memory, bank = first_seed()
    f["x"][4] = DATA
    if fp:
        f["q"][16] = list(struct.pack("<4I", *([0x3F800000] * 4)))
        f["q"][17] = list(struct.pack("<4I", *([0x33000000] * 4)))
    return f, memory, bank


def canonical(fp, scenario, phase="entry"):
    f, _, _ = seed(fp)
    g = guest_seed(scenario)
    for guest, native in zip(ORDER, ENTRY):
        g["gpr"][guest] = f["x"][native]
    for i in range(16):
        g["ymm"][i][:16] = f["q"][16 + i]
    if phase != "entry":
        g["gpr"][1] = 3
        g["ymm"][0][:16] = f["q"][16] if fp else [
            a ^ b for a, b in zip(f["q"][16], f["q"][17])]
        g["rflags"] = 0x206
        if fp:
            g["mxcsr"] = 0x1FA0
        if phase in ("store", "complete"):
            g["gpr"][0] = 0xCAFE123456789003
            g["gpr"][2] = 0
        g["rip"] = SOURCE + {"load": 7, "store": 13, "complete": 17}[phase] - int(fp)
    return g


def body(fp):
    # Retained independent producer fixture, not the runtime's admitted byte array.
    raw = (FIXTURES / ("fp_alias.native" if fp else "alias.native")).read_bytes()
    return raw[12:320]


def export_decode(raw, fp):
    values(raw, 1168, 8, "allocated.export")
    raw = bytes(raw)
    exact(list(struct.unpack_from("<3Q", raw)), [308, 16 if fp else 17, 768], "export header")
    exact(list(raw[24:48]), list((FP_ALIAS if fp else ALIAS).ljust(24, b"\0")), "export source")
    exact(list(raw[48:356]), list(body(fp)), "export native body")
    exact(list(raw[356:560]), [0] * 204, "export body padding")
    sites = []
    for i in range(2):
        words = list(struct.unpack_from("<38Q", raw, 560 + i * 304))
        exact(words[:3], [[148, 7 - int(fp), 0], [296, 13 - int(fp), 1]][i], "export site")
        roots = []
        for role, loc in enumerate(words[3:]):
            tag, at = loc & 255, loc >> 8
            if 16 <= role < 32:
                require(tag == 2 and at < 32, "invalid vector location")
            else:
                require((tag == 1 and at < 31) or (tag == 3 and at % 8 == 0 and at <= 760),
                        "invalid scalar location")
            roots.append((tag, at))
        sites.append(roots)
    return sites


def simulate(fp, pc):
    """Interpret this straight-line finite instruction set; no runtime logic reused."""
    f, memory, _ = seed(fp)
    f["x"][30] = pc
    if fp:
        f["fpcr"] = f["fpsr"] = 0
    spill = [None] * 768
    snapshots = []
    helper_before = None
    for at in range(0, 308, 4):
        w = int.from_bytes(body(fp)[at:at + 4], "little")
        rd, rn, rm = w & 31, (w >> 5) & 31, (w >> 16) & 31
        if at in (148, 296):
            snapshots.append((copy.deepcopy(f), list(spill)))
        if at == 156:
            helper_before = copy.deepcopy(f)
        if w & 0xFFC003FF in (0xD10003FF, 0x910003FF):
            amount = (w >> 10) & 4095
            f["sp"] += -amount if w & 0xFFC003FF == 0xD10003FF else amount
        elif w == 0x6E311E10:
            f["q"][16] = [a ^ b for a, b in zip(f["q"][16], f["q"][17])]
        elif w == 0x4E31D610:
            # Exact sum is 1 + 2^-25; binary32 spacing at1 is2^-23.
            # RN rounds the quarter-ulp increment to1 and records inexact.
            exact(f["q"][16], list(struct.pack("<4I", *([0x3F800000] * 4))), "ADDPS lhs")
            exact(f["q"][17], list(struct.pack("<4I", *([0x33000000] * 4))), "ADDPS rhs")
            f["fpsr"] |= 16
        elif w & 0xFFE0FFE0 == 0xAA0003E0:
            f["x"][rd] = f["x"][rm]
        elif w & 0xFFE0FC00 == 0xCA000000:
            f["x"][rd] = f["x"][rn] ^ f["x"][rm]
        elif w & 0xFFE0FC00 == 0xAB000000:
            result, flags = add_flags(f["x"][rn], f["x"][rm])
            f["x"][rd] = result
            f["nzcv"] = sum(flags[key] << bit for key, bit in (("SF", 31), ("ZF", 30), ("CF", 29), ("OF", 28)))
        elif w & 0xFFC003E0 in (0xF90003E0, 0xF94003E0):
            offset = ((w >> 10) & 4095) * 8
            require(f["sp"] == STACK - 768 and 0 <= offset <= 760, "spill access outside frame")
            if w & 0xFFC003E0 == 0xF90003E0:
                spill[offset:offset + 8] = f["x"][rd].to_bytes(8, "little")
            else:
                require(None not in spill[offset:offset + 8], "uninitialized spill reload")
                f["x"][rd] = int.from_bytes(bytes(spill[offset:offset + 8]), "little")
        elif w & 0xFFFFFC00 == 0xC8DFFC00:
            exact(at, 148, "unregistered load")
            exact(f["x"][rn], DATA, "load address")
            f["x"][rd] = int.from_bytes(bytes(memory[:8]), "little")
        elif w & 0xFFFFFFE0 == 0x9A9F17E0:
            f["x"][rd] = (f["nzcv"] >> 30) & 1
        elif w & 0xFFFFFC00 == 0xB3401C00:
            f["x"][rd] = (f["x"][rd] & ~255) | (f["x"][rn] & 255)
        elif w & 0xFFFFFFE0 == 0xD53B4200:
            f["x"][rd] = f["nzcv"]
        elif w & 0xFFC00000 == 0x3D800000:
            exact(at, 296, "unregistered store")
            exact(f["x"][rn] + ((w >> 10) & 4095) * 16, DATA + 128, "store address")
            memory[128:144] = f["q"][rd]
        elif w == 0xD500401F:
            f["nzcv"] ^= 1 << 29
        else:
            require(w in (0xD503201F, 0xD5033BBF), f"unsupported instruction {w:08x} at{at}")
    exact(f["sp"], STACK, "restored stack")
    # A direct branch to capture must preserve x30: the final map keeps RDI there.
    return snapshots, f, spill, memory, helper_before


def check_spill(actual, expected, label):
    values(actual, 768, 8, label)
    for i, byte in enumerate(expected):
        if byte is not None:
            exact(actual[i], byte, f"{label}[{i}]")


def check_roots(locations, frame, spills, fp, scenario, stage, label):
    guest = canonical(fp, scenario, stage)
    expected = [guest["gpr"][i] for i in ORDER] + [q[:16] for q in guest["ymm"]] + [3, 3, 0]
    for role, ((tag, at), want) in enumerate(zip(locations, expected)):
        actual = (frame["x"][at] if tag == 1 else frame["q"][at] if tag == 2
                  else int.from_bytes(bytes(spills[at:at + 8]), "little"))
        exact(actual, want, label + "." + ROLES[role])


def event_rows(fp, scenario, vm):
    source = FP_ALIAS if fp else ALIAS
    padded = source.ljust(24, b"\0")
    words = list(struct.unpack("<3Q", padded))
    done = int(scenario == "cold")
    sequence = [10, 11] + [19] * 18 + [12 if done else 18]
    rows = []
    for i, event in enumerate(sequence):
        issued, terminal = int(i > 0), i == len(sequence) - 1
        pending = int(i > 0 and not terminal)
        packet = [event, 3, pending, 1, 1, vm, len(source), *words, 2, 0, SOURCE,
                  issued, done if terminal else 0, issued]
        rows.append({"origin": "resident", "event": event, "packet": packet,
                     "pending": pending, "cached": int(terminal), "rejections": 0,
                     "busy": 0, "cancellations": 0})
    return rows


def check(cell):
    if type(cell) is dict and cell.get("case") == "i3-partial":
        return check_partial(cell)
    require(type(cell) is dict and cell.get("case") in ("i3-pressure", "i3-fp", "i3-helper"),
            "expected allocated I3 case")
    fp = cell["case"] == "i3-fp"
    scenario = cell["scenario"]
    require(type(scenario) is str and scenario in CASES, "unsupported pressure scenario")
    completed, faults, walks, retries, error = CASES[scenario]
    vm = uint(cell["vm"], 64, "vm")
    require(vm != 0, "zero VM incarnation")
    rows = event_rows(fp, scenario, vm)
    exact(cell["events"], rows, "events")
    exact(cell["requests"], [rows[1]["packet"]], "requests")
    for name, expected in (("final_pending", 0), ("final_executions", completed), ("final_status", 1)):
        exact(cell[name], expected, name)
    p, a = cell["paging"], cell["allocated"]
    initial, initial_memory, bank = seed(fp)
    exact(a["initial"], initial, "allocated.initial controlled seed")
    exact(a["native_fp_restored"], [initial["fpcr"], initial["fpsr"]], "native FP restoration")
    pc = uint(p["capture_pc"], 64, "capture_pc")
    require(resident_pc(pc), "return gateway outside resident text")
    sites = export_decode(a["export"], fp)
    snapshots, final_frame, final_spills, final_memory, helper_before = simulate(fp, pc)
    if cell["case"] == "i3-helper":
        exact(cell["helper"], helper_expected(helper_before, scenario), "helper")
    raw_spills = values(a["spills"], 3072, 8, "allocated.spills")
    phase = "complete" if completed else "load" if scenario == "load_np" else "store"
    linear = 0 if completed else DATA if phase == "load" else DATA + 128
    for name, expected in (("control", [0x1000, 0, 1, 1, 0]), ("faults", faults),
                           ("walks", walks), ("retries", retries), ("vector", 0 if completed else 14),
                           ("error", error or 0), ("linear", linear),
                           ("rip", 0 if completed else canonical(fp, scenario, phase)["rip"])):
        exact(p[name], expected, "paging." + name)
    before, after = tables(scenario)
    exact(p["tables_before"], before, "tables_before")
    exact(p["tables_after"], after, "tables_after")
    exact(p["guest_before"], canonical(fp, scenario), "guest_before")
    guest = canonical(fp, scenario, phase)
    if not completed:
        guest["control"][1] = linear
    exact(p["guest_after"], guest, "guest_after")
    require(type(p["traces"]) is list and len(p["traces"]) == faults, "wrong trace count")
    for i, trace in enumerate(p["traces"]):
        label = f"traces[{i}]"
        frame, spills = snapshots[i]
        exact(trace["frame"], frame, label + ".frame")
        saved = raw_spills[i * 768:(i + 1) * 768]
        check_spill(saved, spills, label + ".spills")
        check_roots(sites[i], trace["frame"], saved, fp, scenario, "load" if i == 0 else "store", label)
        for name in ("esr", "spsr", "far", "elr", "outcome", "error"):
            uint(trace[name], 64, label + "." + name)
        exact(trace["far"], DATA + (128 if i else 0), label + ".far")
        exact(trace["elr"], CODE + (296 if i else 148), label + ".elr")
        exact(trace["esr"] & 0xFE0007FF, (0x25 << 26) | (1 << 25) | (79 if i else 7),
              label + ".ESR class/cause")
        exact(trace["spsr"] & 0xF00003DF, frame["nzcv"] | 0x3C4, label + ".SPSR")
        terminal = not completed and i == faults - 1
        exact(trace["outcome"], 2 if terminal else 1, label + ".outcome")
        exact(trace["error"], error if terminal else 0, label + ".error")
    for i in range(faults, 3):
        exact(raw_spills[i * 768:(i + 1) * 768], [0] * 768, f"unused spill snapshot{i}")
    if completed:
        check_spill(raw_spills[2304:], final_spills, "final spills")
        check_roots(sites[1], final_frame, raw_spills[2304:], fp, scenario, "complete", "final roots")
    else:
        final_frame = snapshots[-1 if phase == "store" else 0][0]
        final_memory = initial_memory
        exact(raw_spills[2304:], [0] * 768, "no successful completion snapshot")
    exact(a["final_frame"], final_frame, "allocated.final_frame")
    exact(p["memory_after"], final_memory, "memory_after")
    exact(p["bank_after"], bank, "bank_after")
    require(type(cell["executions"]) is list and len(cell["executions"]) == completed, "execution count")
    if completed:
        e = cell["executions"][0]
        for name, expected in (("case", "allocated_fp" if fp else "allocated_xor"), ("status", 1),
                               ("initial", initial), ("captured", final_frame), ("memory_base", DATA),
                               ("memory_before", initial_memory), ("memory_after", final_memory),
                               ("bank_before", bank), ("bank_after", bank), ("object_pc", CODE),
                               ("capture_pc", pc), ("source_generation", 1), ("context_generation", 1)):
            exact(e[name], expected, "execution." + name)


def partial_states():
    prefix = canonical(False, "cold", "complete")
    before = copy.deepcopy(prefix)
    before["gpr"][1], before["gpr"][6], before["gpr"][7] = 2, 0x200000, DATA + 4095
    before["rip"] = SOURCE + 256
    interrupted = copy.deepcopy(before)
    interrupted["gpr"][1], interrupted["gpr"][6], interrupted["gpr"][7] = 1, 0x200001, DATA + 4096
    final = copy.deepcopy(before)
    final["gpr"][1], final["gpr"][6], final["gpr"][7] = 0, 0x200002, DATA + 4097
    final["rip"] += 2
    return prefix, before, interrupted, final


def check_partial(cell):
    """REP is an explicitly admitted resident fixture, not generated FEX REP."""
    exact(cell["scenario"], "cold", "partial scenario")
    p, partial = cell["paging"], cell["partial"]
    prefix, before, interrupted, final = partial_states()
    for name, expected in (("reads", 2), ("writes", 2), ("faults", 1), ("retries", 1),
                           ("phase", 0), ("value", 0x6E), ("read_values", [0x57, 0x6E]),
                           ("written", [0x57, 0x6E]), ("prefix", prefix), ("before", before),
                           ("at_fault", interrupted), ("at_fault_counts", [2, 1, 1, 0x6E])):
        exact(partial[name], expected, "partial." + name)
    exact(p["guest_after"], final, "partial final guest")
    exact(p["walks"], 5, "partial walks include source fetch and write repair")
    _, expected_tables = tables("cold")
    expected_tables[0x3000 + 257 * 8] |= 0x60
    exact(p["tables_after"], expected_tables, "partial table effects")
    store_pc = uint(partial["store_pc"], 64, "partial.store_pc")
    require(resident_pc(store_pc),
            "registered partial store outside resident text")
    t = partial["trace"]
    f = state(t["frame"], "partial.trace.frame")
    exact(f["x"][0], DATA + 4096, "faulted destination operand")
    exact(f["x"][1], 0x6E, "retained second device value")
    require(PRIVATE + 0x50000 <= f["sp"] <= STACK and f["sp"] % 16 == 0,
            "partial native stack outside admitted EL1t stack")
    native, _, _ = first_seed()
    exact(f["fpcr"], native["fpcr"], "partial restored native FPCR")
    exact(f["fpsr"], native["fpsr"], "partial restored native FPSR")
    for name in ("esr", "spsr", "far", "elr", "outcome", "error"):
        uint(t[name], 64, "partial.trace." + name)
    exact(t["far"], DATA + 4096, "partial.trace.far")
    exact(t["elr"], store_pc, "partial.trace exact retry PC")
    exact(t["esr"] & 0xFE0007FF, 0x96000047, "partial native write translation fault")
    exact(t["spsr"] & 0xF00003DF, f["nzcv"] | 0x3C4, "partial saved native PSTATE")
    exact(t["outcome"], 1, "partial retry outcome")
    exact(t["error"], 0, "partial internal repair does not inject #PF")
    rows = event_rows(False, "cold", cell["vm"])
    terminal = copy.deepcopy(rows[-1])
    terminal["event"] = terminal["packet"][0] = 21
    exact(cell["events"], rows + [terminal], "partial lifecycle")

    # Validate the original FEX prefix with its independent oracle after checking
    # every REP-owned difference above. Preserve all other reported fields.
    base = copy.deepcopy(cell)
    base["case"] = "i3-pressure"
    base["events"] = base["events"][:-1]
    base["paging"]["guest_after"] = prefix
    base["paging"]["walks"] = 3
    base["paging"]["tables_after"] = tables("cold")[1]
    check(base)


def fixture_export(fp):
    """Test input from independent producer records; checker never trusts these locations."""
    source = FP_ALIAS if fp else ALIAS
    label = "fp_alias" if fp else "alias"
    roots = {}
    for line in (FIXTURES / (label + ".txt")).read_text().splitlines():
        if line.startswith("NATEMU_ROOT "):
            fields = dict(part.split("=", 1) for part in line.split()[1:])
            roots[(int(fields["guest_offset"]), fields["role"])] = fields["location"]
    out = bytearray(struct.pack("<3Q", 308, len(source), 768))
    out += source.ljust(24, b"\0") + body(fp).ljust(512, b"\0")
    for i, offset in enumerate((7 - int(fp), 13 - int(fp))):
        out += struct.pack("<3Q", (148, 296)[i], offset, i)
        for role in ROLES:
            location = roots[(offset, role)]
            tag, at = ((3, int(location[3:])) if location.startswith("sp+") else
                       (2 if location.startswith("v") else 1, int(location[1:])))
            out += struct.pack("<Q", tag | at << 8)
    return list(out)


def helper_expected(before, scenario):
    if scenario == "load_np":
        zero = {"x": [0] * 31, "q": [[0] * 16 for _ in range(32)],
                "sp": 0, "nzcv": 0, "fpcr": 0, "fpsr": 0}
        return {"calls": 0, "outputs": 0, "before": zero,
                "staged": copy.deepcopy(zero), "poisoned": copy.deepcopy(zero)}
    staged = copy.deepcopy(before)
    staged["x"][20] = (before["nzcv"] >> 30) & 1
    staged["x"][5] = (before["x"][5] & ~255) | staged["x"][20]
    poisoned = {"x": [0x5A] * 31, "q": [[0xA5] * 16 for _ in range(32)],
                "sp": PRIVATE + 0x64000 - 800, "nzcv": 0xF0000000,
                "fpcr": 0x400000, "fpsr": 0x8000001}
    return {"calls": 1, "outputs": 2, "before": before, "staged": staged, "poisoned": poisoned}


def synthetic(fp, scenario, helper=False):
    done, faults, walks, retries, error = CASES[scenario]
    pc, vm = PRIVATE + 0xA0080, 0x123456789ABCDEF
    rows = event_rows(fp, scenario, vm)
    initial, memory, bank = seed(fp)
    snapshots, final, spill, after, helper_before = simulate(fp, pc)
    saved = [0] * 3072
    traces = []
    for i in range(faults):
        frame, snapshot = snapshots[i]
        saved[i * 768:(i + 1) * 768] = [0 if b is None else b for b in snapshot]
        terminal = not done and i == faults - 1
        traces.append({"frame": frame, "esr": (0x25 << 26) | (1 << 25) | (79 if i else 7),
                       "spsr": frame["nzcv"] | 0x3C4, "far": DATA + (128 if i else 0),
                       "elr": CODE + (296 if i else 148), "outcome": 2 if terminal else 1,
                       "error": error if terminal else 0})
    phase = "complete" if done else "load" if scenario == "load_np" else "store"
    if done:
        saved[2304:] = [0 if b is None else b for b in spill]
    else:
        final, after = snapshots[faults - 1][0], memory
    linear = 0 if done else DATA + (128 if phase == "store" else 0)
    guest = canonical(fp, scenario, phase)
    if not done:
        guest["control"][1] = linear
    before_tables, after_tables = tables(scenario)
    executions = []
    if done:
        executions.append({"case": "allocated_fp" if fp else "allocated_xor", "status": 1,
                           "initial": initial, "captured": final, "memory_base": DATA,
                           "memory_before": memory, "memory_after": after, "bank_before": bank,
                           "bank_after": bank, "object_pc": CODE, "capture_pc": pc,
                           "source_generation": 1, "context_generation": 1})
    cell = {"case": "i3-fp" if fp else "i3-pressure", "scenario": scenario, "vm": vm,
            "events": rows, "requests": [rows[1]["packet"]], "executions": executions,
            "final_pending": 0, "final_status": 1, "final_executions": done,
            "allocated": {"initial": initial, "final_frame": final, "spills": saved,
                          "export": fixture_export(fp),
                          "native_fp_restored": [initial["fpcr"], initial["fpsr"]]},
            "paging": {"capture_pc": pc, "guest_before": canonical(fp, scenario), "guest_after": guest,
                       "control": [0x1000, 0, 1, 1, 0], "faults": faults, "walks": walks,
                       "retries": retries, "vector": 0 if done else 14, "error": error or 0,
                       "linear": linear, "rip": 0 if done else guest["rip"], "traces": traces,
                       "tables_before": before_tables, "tables_after": after_tables,
                       "memory_after": after, "bank_after": bank}}
    if helper:
        require(not fp, "FP helper not in this profile")
        cell["case"] = "i3-helper"
        cell["helper"] = helper_expected(helper_before, scenario)
    return cell


def synthetic_partial():
    cell = synthetic(False, "cold")
    cell["case"] = "i3-partial"
    prefix, before, interrupted, final = partial_states()
    cell["paging"]["guest_after"] = final
    cell["paging"]["walks"] = 5
    cell["paging"]["tables_after"][0x3000 + 257 * 8] |= 0x60
    terminal = copy.deepcopy(cell["events"][-1])
    terminal["event"] = terminal["packet"][0] = 21
    cell["events"].append(terminal)
    frame, _, _ = first_seed()
    frame["x"][0], frame["x"][1], frame["sp"] = DATA + 4096, 0x6E, STACK - 4096
    store_pc = PRIVATE + 0xA0280
    cell["partial"] = {"prefix": prefix, "before": before, "at_fault": interrupted,
                       "at_fault_counts": [2, 1, 1, 0x6E], "reads": 2, "writes": 2,
                       "faults": 1, "retries": 1, "phase": 0, "value": 0x6E,
                       "read_values": [0x57, 0x6E], "written": [0x57, 0x6E],
                       "store_pc": store_pc,
                       "trace": {"frame": frame, "esr": 0x96000047, "spsr": frame["nzcv"] | 0x3C4,
                                 "far": DATA + 4096, "elr": store_pc, "outcome": 1, "error": 0}}
    return cell


def self_test():
    cases = {(fp, scenario): synthetic(fp, scenario) for fp in (False, True) for scenario in CASES}
    for cell in cases.values():
        check(cell)
    changes = [
        ("old RAX lost", False, "load_np", ("paging", "guest_after", "gpr", 0), 0xCAFE123456789003),
        ("new RAX lost", False, "store_ro", ("paging", "guest_after", "gpr", 0), DATA),
        ("R12 entry clobber", False, "cold", ("allocated", "initial", "x", 16), CODE),
        ("spilled RAX corrupted", False, "load_np", ("allocated", "spills", 0), 1),
        ("final spills overwritten", False, "cold", ("allocated", "spills", 2304 + 384), 0),
        ("wrong load map", False, "load_np", ("allocated", "export", 584), 1),
        ("wrong store map", False, "store_ro", ("allocated", "export", 889), 0),
        ("missing precision sticky", True, "load_np", ("paging", "guest_after", "mxcsr"), 0x1F80),
        ("phantom invalid sticky", True, "cold", ("paging", "guest_after", "mxcsr"), 0x1FA1),
        ("FP result rounded up", True, "cold", ("paging", "guest_after", "ymm", 0, 0), 1),
        ("FP status not captured", True, "store_ro", ("paging", "traces", 1, "frame", "fpsr"), 0),
        ("native FP not restored", True, "cold", ("allocated", "native_fp_restored", 0), 0),
        ("wrong guest boundary", True, "load_np", ("paging", "guest_after", "rip"), SOURCE + 7),
        ("store before fault", False, "store_ro", ("paging", "memory_after", 128), 0),
        ("bank clobber", True, "cold", ("paging", "bank_after", 0), 0),
        ("YMM high clobber", True, "cold", ("paging", "guest_after", "ymm", 15, 31), 0),
        ("native scratch clobber", False, "load_np", ("paging", "traces", 0, "frame", "x", 18), 0),
        ("return trailer destroys live RDI", False, "cold", ("allocated", "final_frame", "x", 30), PRIVATE + 0xA0080),
        ("missing stream ack", False, "cold", ("events",), []),
        ("extra execution", False, "cold", ("final_executions",), 2),
    ]
    for label, fp, scenario, path, value in changes:
        bad = copy.deepcopy(cases[(fp, scenario)])
        target = bad
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        try:
            check(bad)
        except ValueError:
            continue
        raise ValueError("failed to reject corruption: " + label)
    helpers = {scenario: synthetic(False, scenario, helper=True) for scenario in CASES}
    for cell in helpers.values():
        check(cell)
    helper_changes = [
        ("helper executed before load fault", "load_np", ("helper", "calls"), 1),
        ("helper called twice", "cold", ("helper", "calls"), 2),
        ("missing helper output", "store_ro", ("helper", "outputs"), 1),
        ("helper prestate lost RAX", "cold", ("helper", "before", "x", 4), DATA),
        ("helper staged unrelated GPR", "cold", ("helper", "staged", "x", 12), 0),
        ("helper skipped SETZ", "store_ro", ("helper", "staged", "x", 5), 2),
        ("poison did not cover GPR", "cold", ("helper", "poisoned", "x", 18), 0),
        ("poison did not cover high Q", "cold", ("helper", "poisoned", "q", 31, 15), 0),
        ("poison did not cover flags", "cold", ("helper", "poisoned", "nzcv"), 0),
        ("poison did not cover FPCR", "cold", ("helper", "poisoned", "fpcr"), 0),
        ("poison leaked into resumed frame", "store_ro", ("paging", "traces", 1, "frame", "x", 12), 0x5A),
    ]
    for label, scenario, path, value in helper_changes:
        bad = copy.deepcopy(helpers[scenario])
        target = bad
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        try:
            check(bad)
        except ValueError:
            continue
        raise ValueError("failed to reject corruption: " + label)
    partial = synthetic_partial()
    check(partial)
    partial_changes = [
        ("device read replayed", ("partial", "reads"), 3),
        ("second value recreated", ("partial", "written", 1), 0x85),
        ("first store replayed", ("partial", "writes"), 3),
        ("lost partial counter", ("partial", "at_fault", "gpr", 1), 2),
        ("lost partial source", ("partial", "at_fault", "gpr", 6), 0x200000),
        ("lost partial destination", ("partial", "at_fault", "gpr", 7), DATA + 4095),
        ("read repeated before fault", ("partial", "at_fault_counts", 0), 3),
        ("completion skipped remaining write", ("paging", "guest_after", "gpr", 1), 1),
        ("REP changed flags", ("paging", "guest_after", "rflags"), 0x202),
        ("REP reset earlier vector", ("paging", "guest_after", "ymm", 0, 0), 0x11),
        ("internal repair changed CR2", ("paging", "guest_after", "control", 1), DATA + 4096),
        ("missing second page dirty", ("paging", "tables_after", 0x3000 + 257 * 8), 7),
        ("wrong retry operand", ("partial", "trace", "frame", "x", 1), 0x57),
        ("retry at wrong PC", ("partial", "trace", "elr"), PRIVATE + 0x2004),
        ("prior prefix rewound", ("partial", "prefix", "gpr", 0), DATA),
    ]
    for label, path, value in partial_changes:
        bad = copy.deepcopy(partial)
        target = bad
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        try:
            check(bad)
        except ValueError:
            continue
        raise ValueError("failed to reject corruption: " + label)
    count = len(changes) + len(helper_changes) + len(partial_changes)
    print(f"PASS: six pressure/FP + three helper + one resident REP cell and {count} corruption controls")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if not args.paths and not args.self_test:
        parser.error("provide JSON files, or --self-test")
    if args.self_test:
        self_test()
    failures = 0
    for path in args.paths:
        try:
            check(json.loads(path.read_text()))
            print(f"PASS: {path}")
        except (OSError, ValueError, KeyError, TypeError, struct.error) as error:
            failures += 1
            print(f"FAIL: {path}: {error}", file=sys.stderr)
    return int(failures != 0)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Independent oracle for finite I4 entry, faults, remapping and code retirement.

Checks the externally declared participant set and per-participant seed vectors,
then reuses I1's instruction semantics checker for entry/state isolation.
The faults profile additionally checks exact pre-load state and an independent
ownership/FIFO trace model. Remapping additionally checks old/new memory effects,
per-owner invalidation acknowledgements and retained-route release ordering.
Composition additionally checks live compiler identities, source replacement,
stale entry/reuse rejection and the retired code bytes. Events adds one identified
level request per CPU, coalesced notifications, eligibility and canonical handoff.
It does not establish native IRQ arrival, sleeping waits, IDT/APIC delivery or fairness.
"""

import argparse
import copy
import json
from pathlib import Path
import sys

from check_i1 import add_flags, check as check_instruction
from check_i1 import equal, require, state, uint, values


PRIVATE = 0x0000800040000000
DATA = 0x100000
CODE = PRIVATE + 0x30000
CODE_NEXT = CODE + 4096
MASK = (1 << 64) - 1
X86_XOR = bytes.fromhex("660fefc14801d1488b030f94c2f30f7f07")
X86_OR = bytes.fromhex("660febc14801d1488b030f94c2f30f7f07")
NATIVE_OR = bytes.fromhex("101eb14efb0005cafa0005abe7031aaac4fcdfc81f2003d5"
                          "f4179f9a851e40b3bf3b03d57001803d1f4000d5")
RET = bytes.fromhex("c0035fd6")


def seed(cpu):
    """Declared fixture inputs, independent of guest/host-reported seed fields."""
    lhs, rhs = [(0, 0), (MASK, 1), ((1 << 63) - 1, 1), (1, 2)][cpu]
    initial = {
        "x": [0x1234000000000000 + register * 0x01010101 for register in range(31)],
        "q": [[(register * 17 + byte * 3 + 1) & 255 for byte in range(16)]
              for register in range(32)],
        "sp": PRIVATE + 0x54000,
        "nzcv": 0xA0000000,
        "fpcr": 0x01000000,
        "fpsr": 0x08000001,
    }
    initial["x"][5] = rhs
    initial["x"][6] = DATA + cpu * 256
    initial["x"][7] = lhs
    initial["x"][11] = DATA + cpu * 256 + 128
    initial["x"][28] = PRIVATE + 0x40000
    bank = [((offset * 37 + 19) & 255) ^ cpu for offset in range(512)]
    data = [(offset * 13 + 7) & 255 for offset in range(256)]
    data[:8] = (0xCAFE123456789000 + cpu).to_bytes(8, "little")
    return initial, bank, data


def check(record, expected_vcpus=None, expected_delay=None, expected_profile=None):
    require(type(record) is dict, "expected one I4 result object")
    if record.get("profile") == "i4-pressure":
        from check_i4_pressure import check as check_pressure
        return check_pressure(record, expected_vcpus, expected_delay, expected_profile)
    require(record["profile"] in ("i4-entry", "i4-faults", "i4-remap", "i4-compose", "i4-events"), "unknown I4 profile")
    if expected_profile is not None:
        equal(record["profile"], expected_profile, "requested I4 profile")
    n = uint(record["configured_vcpus"], 32, "configured_vcpus")
    require(1 <= n <= 4, "I4 entry profile admits 1..4 vCPUs")
    equal(uint(record["tested_vcpus"], 32, "tested_vcpus"), n, "tested_vcpus")
    if expected_vcpus is not None:
        equal(n, expected_vcpus, "requested vCPU count")
    delay = uint(record["delayed_owner_ms"], 32, "delayed_owner_ms")
    require(delay <= 1000, "owner delay outside admitted bound")
    if expected_delay is not None:
        equal(delay, expected_delay, "requested owner delay")
    for field in ("ready", "finished"):
        equal(uint(record[field], 64, field), (1 << n) - 1, field)
    cells = record["cells"]
    require(type(cells) is list and len(cells) == n, "one result per participant required")
    seen = set()
    for cell in cells:
        cpu = uint(cell["id"], 32, "participant id")
        require(cpu < n and cpu not in seen, "duplicate or unconfigured participant")
        seen.add(cpu)
        equal(cell["case"], "live_load", "participant fixture")
        equal(uint(cell["memory_base"], 64, "memory_base"), DATA + cpu * 256, "participant data base")
        equal(uint(cell["object_pc"], 64, "object_pc"), CODE, "object_pc")
        capture = uint(cell["capture_pc"], 64, "capture_pc")
        require(PRIVATE <= capture < PRIVATE + 0x10000 and capture % 4 == 0,
                "capture target outside resident text")
        initial, bank, data = seed(cpu)
        actual = state(cell["initial"], "initial")
        for field in initial:
            equal(actual[field], initial[field], f"CPU{cpu} initial {field}")
        equal(values(cell["bank_before"], 512, 8, "bank_before"), bank, "participant bank seed")
        equal(values(cell["memory_before"], 256, 8, "memory_before"), data, "participant data seed")
        check_instruction(cell)
    equal(seen, set(range(n)), "participant set")
    if record["profile"] != "i4-entry":
        check_faults(record, n)
        if record["profile"] in ("i4-remap", "i4-compose", "i4-events"):
            check_remap(record, n)
        if record["profile"] in ("i4-compose", "i4-events"):
            check_source(record, n)
        if record["profile"] == "i4-events":
            check_events(record, n)
    else:
        for field, expected in (("trace", []), ("faults", []), ("retained", 0)):
            if field in record:
                require(type(record[field]) is type(expected) and record[field] == expected,
                        "entry profile unexpectedly has " + field)


def load_fault_frame(cpu, capture_pc):
    frame, _, _ = seed(cpu)
    lhs, rhs = frame["x"][7], frame["x"][5]
    result, flags = add_flags(lhs, rhs)
    frame["x"][16], frame["x"][30] = CODE, capture_pc
    frame["x"][7] = frame["x"][26] = result
    frame["x"][27] = lhs ^ rhs
    frame["q"][16] = [a ^ b for a, b in zip(frame["q"][16], frame["q"][17])]
    frame["nzcv"] = (flags["SF"] << 31 | flags["ZF"] << 30 |
                     flags["CF"] << 29 | flags["OF"] << 28)
    return frame


def check_faults(record, n):
    faults = record["faults"]
    require(type(faults) is list and len(faults) == n, "one native fault per participant")
    cells = {cell["id"]: cell for cell in record["cells"]}
    seen = set()
    for fault in faults:
        cpu = uint(fault["id"], 32, "fault participant")
        require(cpu < n and cpu not in seen, "duplicate or invalid fault participant")
        seen.add(cpu)
        equal(uint(fault["faults"], 64, "fault count"), 1, "fault count")
        expected = load_fault_frame(cpu, cells[cpu]["capture_pc"])
        actual = state(fault["frame"], "fault frame")
        for field in expected:
            equal(actual[field], expected[field], f"CPU{cpu} fault {field}")
        for field in ("old_gpa", "far"):
            equal(uint(fault[field], 64, field), DATA + cpu * 256, field)
        equal(uint(fault["pc"], 64, "fault pc"), CODE + 16, "fault site")
        esr = uint(fault["esr"], 64, "fault esr")
        equal(esr >> 26, 0x25, "same-EL data abort")
        equal(esr & (1 << 6), 0, "read fault")
        require(esr & 63 in range(4, 8), "expected stage-1 translation fault")
        equal(esr & ((1 << 10) | (1 << 8) | (1 << 7)), 0, "valid direct fault address")
        spsr = uint(fault["spsr"], 64, "fault spsr")
        equal(spsr & 0xF0000000, expected["nzcv"], "fault SPSR flags")
        equal(spsr & 0x3DF, 0x3C4, "fault SPSR masked EL1t")
    equal(seen, set(range(n)), "fault participant set")
    equal(uint(record["retained"], 64, "retained"), 0, "all references released")
    check_fault_trace(record["trace"], n, record["profile"] in ("i4-remap", "i4-compose", "i4-events"),
                      record["profile"] in ("i4-compose", "i4-events"), record["profile"] == "i4-events")


def check_fault_trace(trace, n, remap=False, compose=False, events=False):
    """Model ownership as sets and FIFO history, independently of runtime fields."""
    require(type(trace) is list and 0 < len(trace) <= 128, "bounded nonempty fault trace")
    admitted, active, retained, detached, completed, released = (set() for _ in range(6))
    queued = []
    phase = "open"
    servicing = None
    delayed = False
    acknowledgements, new_entries, new_completed, dirty = (set() for _ in range(4))
    mutated, reclaimed = False, False
    last_release = None
    published = False
    rejected = set()
    posted, renotified, delivered, cleared, shadow_cleared = (set() for _ in range(5))
    for word in trace:
        uint(word, 64, "trace word")
        event, cpu = word >> 8, word & 255
        require(cpu < n, "trace participant outside admitted set")
        if events and event == 18:
            require(phase == "open" and not admitted and cpu not in posted,
                    "initial event must arrive before admission, once per CPU")
            posted.add(cpu)
        elif events and event == 19:
            require(phase == "compiling" and cpu in posted and cpu not in renotified,
                    "repeated notification must occur during stopped replacement compilation")
            renotified.add(cpu)
        elif events and event == 20:
            require(cpu in posted and cpu not in delivered and cpu not in active,
                    "event delivery without request, duplicated or during active execution")
            if cpu in (1, 3):
                require(phase == "open" and cpu not in admitted,
                        "initially eligible event must precede first native entry")
                if cpu == 1:
                    require(cpu in shadow_cleared, "delivery while fixture shadow is set")
            else:
                require(phase == "reopened" and cpu in new_completed and cpu in cleared,
                        "IF/priority-blocked event delivered before final boundary/control clear")
            delivered.add(cpu)
        elif events and event == 21:
            require(cpu in new_completed and cpu not in cleared,
                    "fixture controls cleared before completion or twice")
            cleared.add(cpu)
        elif events and event == 22:
            require(cpu == 1 and phase == "open" and cpu not in admitted
                    and cpu not in delivered and cpu not in shadow_cleared,
                    "fixture shadow must clear once before any guest instruction")
            shadow_cleared.add(cpu)
        elif event == 1:
            if events:
                equal(posted, set(range(n)), "all initial requests precede admission")
                if cpu in (1, 3):
                    require(cpu in delivered, "guest entry skipped eligible initial event")
            require(phase == "open" and cpu not in admitted, "invalid or duplicate admission")
            admitted.add(cpu)
            active.add(cpu)
            retained.add(cpu)
        elif event == 2:
            equal(admitted, set(range(n)), "all grants admitted before faults")
            require(phase in ("open", "closing") and cpu in active,
                    "detach requires active grant before exclusive service")
            if cpu == n - 1:
                require(delayed, "last grant detached before forced delay point")
            active.remove(cpu)
            detached.add(cpu)
            queued.append(cpu)
            phase = "closing"
        elif event == 3:
            require(cpu == 0 and phase == "closing" and not active,
                    "exclusive service began before every grant drained")
            equal(detached, set(range(n)), "all fault frames queued")
            phase = "exclusive"
        elif event == 4:
            require(phase == "exclusive" and servicing is None and queued,
                    "overlapping service or service without closed admission")
            equal(cpu, queued.pop(0), "FIFO service")
            require(cpu in retained and cpu not in completed, "service lost reference or duplicated")
            servicing = cpu
        elif event == 5:
            require(phase == "exclusive" and servicing == cpu and cpu not in completed,
                    "completion outside its selected service")
            completed.add(cpu)
            servicing = None
        elif event == 6:
            equal(completed, set(range(n)), "reference release after all old fragments complete")
            require(cpu in retained and cpu not in released, "duplicate reference release")
            if remap:
                require(mutated and phase == "reopened", "old route release before covering invalidation")
                if cpu == n - 1 and n > 1:
                    require(new_entries, "delayed old reference did not overlap a new admission")
            if compose:
                require(cpu in rejected, "old reference released before stale entry/reuse rejection")
            retained.remove(cpu)
            released.add(cpu)
            last_release = cpu
        elif event == 7:
            require(not delayed and cpu == n - 1 and cpu in active,
                    "invalid delayed-grant point")
            equal(detached, set(range(n - 1)), "all peers detached before delayed grant proceeds")
            delayed = True
        elif remap and event == 8:
            require(not mutated and phase == "exclusive" and not active and servicing is None,
                    "mutation without exclusive drained ownership")
            equal(completed, set(range(n)), "old effects must complete before conflicting mutation")
            require(not queued, "mutation before queued effects complete")
            mutated = True
            phase = "compiling" if compose else "invalidating"
        elif remap and event == 9:
            require(phase == "invalidating" and cpu not in acknowledgements,
                    "duplicate or premature mapping acknowledgement")
            if cpu == n - 1:
                equal(acknowledgements, set(range(n - 1)), "last participant acknowledges after peers")
            acknowledgements.add(cpu)
        elif remap and event == 10:
            require(phase == "invalidating", "duplicate or premature admission reopen")
            equal(acknowledgements, set(range(n)), "all covering maintenance before reopen")
            phase = "reopened"
        elif remap and event == 11:
            require(phase == "reopened" and cpu in released and cpu not in new_entries,
                    "new entry before invalidation/ref release or duplicated")
            new_entries.add(cpu)
            active.add(cpu)
        elif remap and event == 12:
            require(cpu in active and cpu in new_entries and cpu in dirty and cpu not in new_completed,
                    "new completion without one admitted dirty-repaired execution")
            active.remove(cpu)
            new_completed.add(cpu)
        elif remap and event == 14:
            require(not reclaimed and not retained and cpu == last_release and phase == "reopened",
                    "old-route reclamation before last reference release or duplicated")
            equal(released, set(range(n)), "all old references released before reclamation")
            reclaimed = True
        elif remap and event == 15:
            require(phase == "reopened" and cpu in active and cpu not in dirty,
                    "dirty fault outside an admitted new access or duplicated")
            dirty.add(cpu)
        elif compose and event == 16:
            require(cpu == 0 and phase == "compiling" and not published,
                    "replacement publication outside stopped mutation or duplicated")
            if events:
                equal(renotified, set(range(n)), "all repeated notifications precede publication")
            published = True
            phase = "invalidating"
        elif compose and event == 17:
            require(phase == "reopened" and cpu in retained and cpu not in rejected,
                    "stale entry/reuse check without a retained old reference or duplicated")
            rejected.add(cpu)
        else:
            raise ValueError(f"unknown fault trace event {event}")
    equal(phase, "reopened" if remap else "exclusive", "final service phase")
    equal(completed, set(range(n)), "all services completed")
    equal(released, set(range(n)), "all references released by owners")
    require(not active and not retained and not queued and servicing is None,
            "live ownership remains at termination")
    if remap:
        equal(new_entries, set(range(n)), "all participants entered replacement mapping")
        equal(new_completed, set(range(n)), "all replacement executions completed")
        require(mutated and reclaimed, "missing mutation or last-reference reclamation")
    if compose:
        require(published, "replacement code never published")
        equal(rejected, set(range(n)), "all stale target/reuse attempts rejected")

    if events:
        equal(posted, set(range(n)), "one initial event per CPU")
        equal(renotified, set(range(n)), "one repeated notification per CPU")
        equal(delivered, set(range(n)), "one canonical event handoff per CPU")
        equal(cleared, set(range(n)), "all fixture controls cleared")
        equal(shadow_cleared, {1} if n > 1 else set(), "fixture shadow clear")


def replacement_seed(cpu):
    _, _, old = seed(cpu)
    data = [byte ^ 0x5A for byte in old]
    data[:8] = (0xD00D987654321000 + cpu).to_bytes(8, "little")
    return data


def store_fault_frame(initial, memory, use_or=False):
    frame = copy.deepcopy(initial)
    lhs, rhs = initial["x"][7], initial["x"][5]
    result, flags = add_flags(lhs, rhs)
    frame["x"][7] = frame["x"][26] = result
    frame["x"][27] = lhs ^ rhs
    frame["x"][4] = int.from_bytes(bytes(memory[:8]), "little")
    frame["x"][5] = (rhs & ~255) | flags["ZF"]
    frame["x"][20] = flags["ZF"]
    frame["q"][16] = [(a | b) if use_or else (a ^ b)
                      for a, b in zip(frame["q"][16], frame["q"][17])]
    frame["nzcv"] = (flags["SF"] << 31 | flags["ZF"] << 30 |
                     flags["CF"] << 29 | flags["OF"] << 28)
    return frame


def check_remap(record, n):
    compose = record["profile"] in ("i4-compose", "i4-events")
    code = CODE_NEXT if compose else CODE
    old_cells = {cell["id"]: cell for cell in record["cells"]}
    new_cells = record["new_cells"]
    require(type(new_cells) is list and len(new_cells) == n, "one replacement execution per participant")
    seen = set()
    for cell in new_cells:
        cpu = uint(cell["id"], 32, "new participant id")
        require(cpu < n and cpu not in seen, "duplicate or invalid new participant")
        seen.add(cpu)
        old = old_cells[cpu]
        equal(cell["case"], "live_or" if compose else "live_load", "replacement fixture")
        equal(uint(cell["dirty_faults"], 64, "dirty_faults"), 1, "one native dirty repair")
        equal(uint(cell["memory_base"], 64, "new memory_base"), DATA + cpu * 256,
              "same guest address after remap")
        equal(uint(cell["object_pc"], 64, "new object_pc"), code, "replacement native slot")
        equal(uint(cell["capture_pc"], 64, "new capture_pc"), old["capture_pc"], "capture continuity")
        before = state(cell["initial"], "replacement initial")
        expected_initial = copy.deepcopy(old["captured"])
        expected_initial["x"][16] = code
        for field in before:
            equal(before[field], expected_initial[field], "old/new state continuity " + field)
        equal(values(cell["memory_before"], 256, 8, "replacement memory"), replacement_seed(cpu),
              "distinct replacement physical data")
        _, bank, _ = seed(cpu)
        equal(values(cell["bank_before"], 512, 8, "replacement bank"), bank, "same private bank")
        check_instruction(cell)
        fault = cell["dirty_fault"]
        expected = store_fault_frame(expected_initial, replacement_seed(cpu), compose)
        actual = state(fault["frame"], "dirty fault frame")
        for field in expected:
            equal(actual[field], expected[field], f"CPU{cpu} dirty fault {field}")
        equal(uint(fault["far"], 64, "dirty FAR"), DATA + cpu * 256 + 128, "dirty store address")
        equal(uint(fault["pc"], 64, "dirty PC"), code + 36, "dirty store site")
        esr = uint(fault["esr"], 64, "dirty ESR")
        equal(esr >> 26, 0x25, "dirty same-EL data abort")
        equal(esr & (1 << 6), 1 << 6, "dirty write fault")
        require(esr & 63 in range(12, 16), "dirty permission fault required")
        equal(esr & ((1 << 10) | (1 << 8) | (1 << 7)), 0, "dirty fault provenance")
        spsr = uint(fault["spsr"], 64, "dirty SPSR")
        equal(spsr & 0xF0000000, expected["nzcv"], "dirty direct carry flags")
        equal(spsr & 0x3DF, 0x3C4, "dirty masked EL1t")
    coherence = record["coherence"]
    require(type(coherence) is dict, "coherence summary required")
    for field, expected in {"generation": 2, "acknowledgements": (1 << n) - 1,
                            "active": 0, "new_entries": (1 << n) - 1,
                            "new_completed": (1 << n) - 1, "new_references": 0,
                            "reclaimed": 1, "leaf": (DATA + 4096) | 0x67}.items():
        equal(uint(coherence[field], 64, "coherence " + field), expected, "coherence " + field)


def compiler_requests(vm):
    requests = []
    for serial, source in enumerate((X86_XOR, X86_XOR, X86_OR), 1):
        padded = source + bytes(24 - len(source))
        words = [int.from_bytes(padded[offset:offset + 8], "little") for offset in (0, 8, 16)]
        requests.append([11, 3, serial, 2 if serial == 3 else 1, 1, vm,
                         17, *words, 1, 0, DATA + 8192, serial, 0, serial])
    return requests


def check_source(record, n):
    source = record["source"]
    require(type(source) is dict, "source retirement summary required")
    for field, expected in {"old_eligible": 0, "published": 2, "stale_replies": 1,
                            "stale_entries": (1 << n) - 1, "reuse_blocked": (1 << n) - 1}.items():
        equal(uint(source[field], 64, "source " + field), expected, "source " + field)
    for field, expected in (("bytes", X86_OR), ("old_code", bytes(44) + RET),
                            ("new_code", NATIVE_OR + RET)):
        equal(values(source[field], len(expected), 8, field), list(expected), field)
    requests = record["requests"]
    require(type(requests) is list and len(requests) == 3, "three actual compiler requests required")
    for i, request in enumerate(requests):
        values(request, 16, 64, f"request[{i}]")
    vm = requests[0][5]
    require(vm != 0, "compiler requests need nonzero VM identity")
    for i, (actual, expected) in enumerate(zip(requests, compiler_requests(vm))):
        equal(actual, expected, f"request[{i}] identity/source/sequence")


def event_packet(cpu):
    """Independent canonical state from declared inputs and x86 arithmetic.

    Reuse the earlier preservation oracle, never a reported packet/native result.
    Initial boundary3 precedes all instructions; boundary2 follows both bodies.
    """
    from check_i3_paging import copy_live, guest_seed
    guest = guest_seed("cold")
    initial, _, memory = seed(cpu)
    if cpu in (1, 3):
        copy_live(guest, initial)
        return guest
    old = store_fault_frame(initial, memory)
    new = store_fault_frame(old, replacement_seed(cpu), True)
    copy_live(guest, new)
    _, flags = add_flags(old["x"][7], old["x"][5])
    guest["rflags"] = 0x202 | sum(flags[name] << bit for name, bit in
        (("CF", 0), ("PF", 2), ("AF", 4), ("ZF", 6), ("SF", 7), ("OF", 11)))
    guest["rip"] = DATA + 8192 + 17
    return guest


def check_events(record, n):
    from check_i3_paging import exact
    events = record["events"]
    require(type(events) is list and len(events) == n, "one event result per participant")
    seen = set()
    for event in events:
        cpu = uint(event["id"], 32, "event participant")
        require(cpu < n and cpu not in seen, "duplicate or unconfigured event participant")
        seen.add(cpu)
        for field, expected in {"pending": 0, "token_state": 2, "notifications": 2,
                                "consumed": 1, "deliveries": 1, "shadow": 0, "priority": 0,
                                "vector": 64 + cpu,
                                "boundary": 3 if cpu in (1, 3) else 2}.items():
            equal(uint(event[field], 64, field), expected, f"CPU{cpu} event {field}")
        waits = uint(event["wait_checks"], 64, "wait checks")
        if cpu == 0:
            require(waits > 0, "compiler owner must recheck pending events while waiting")
        # CPU0/2 remain pending through closure, incomplete faults and active
        # second-body execution. CPU1/3 consume before any native instruction.
        expected_blocked = [2 | 4 | 32 | 64, 8, 2 | 16 | 32 | 64, 0][cpu]
        equal(uint(event["blocked"], 64, "blocked"), expected_blocked | int(waits > 0),
              f"CPU{cpu} exact eligibility/wait blockers")
        exact(event["packet"], event_packet(cpu), f"CPU{cpu} canonical event packet")
    equal(seen, set(range(n)), "event participant set")


def known_result(n, delay):
    """Known ADD answers plus the fixture's simple bytewise XOR for self-tests."""
    cells = []
    for cpu in range(n):
        initial, bank, data = seed(cpu)
        captured = copy.deepcopy(initial)
        captured["x"][16] = CODE
        captured["x"][30] = PRIVATE + 0x400
        captured["x"][4] = 0xCAFE123456789000 + cpu
        captured["x"][5] = [1, 1, 0, 0][cpu]
        captured["x"][7] = captured["x"][26] = [0, 0, 1 << 63, 3][cpu]
        captured["x"][20] = [1, 1, 0, 0][cpu]
        captured["x"][27] = [0, MASK - 1, (1 << 63) - 2, 3][cpu]
        captured["nzcv"] = [0x60000000, 0x40000000, 0xB0000000, 0x20000000][cpu]
        captured["q"][16] = [a ^ b for a, b in zip(initial["q"][16], initial["q"][17])]
        after = list(data)
        after[128:144] = captured["q"][16]
        cells.append({"id": cpu, "case": "live_load", "status": 1,
                      "initial": initial, "captured": captured,
                      "memory_base": DATA + cpu * 256,
                      "memory_before": data, "memory_after": after,
                      "bank_before": bank, "bank_after": list(bank),
                      "object_pc": CODE, "capture_pc": PRIVATE + 0x400})
    return {"profile": "i4-entry", "configured_vcpus": n, "tested_vcpus": n,
            "delayed_owner_ms": delay, "ready": (1 << n) - 1,
            "finished": (1 << n) - 1, "cells": cells}


def known_fault_result(n, delay):
    record = known_result(n, delay)
    record["profile"] = "i4-faults"
    record["retained"] = 0
    trace = [(1 << 8) | cpu for cpu in range(n)]
    order = list(reversed(range(n - 1))) + [n - 1]
    trace += [(2 << 8) | cpu for cpu in order[:-1]]
    trace += [(7 << 8) | (n - 1), (2 << 8) | (n - 1), 3 << 8]
    for cpu in order:
        trace += [(4 << 8) | cpu, (5 << 8) | cpu]
    trace += [(6 << 8) | cpu for cpu in reversed(range(n))]
    record["trace"] = trace
    faults = []
    for cpu in range(n):
        frame = load_fault_frame(cpu, record["cells"][cpu]["capture_pc"])
        faults.append({"id": cpu, "faults": 1, "old_gpa": DATA + cpu * 256,
                       "frame": frame, "esr": (0x25 << 26) | 7,
                       "far": DATA + cpu * 256, "pc": CODE + 16,
                       "spsr": frame["nzcv"] | 0x3C4})
    record["faults"] = faults
    return record


def known_remap_result(n, delay, compose=False):
    record = known_fault_result(n, delay)
    record["profile"] = "i4-compose" if compose else "i4-remap"
    code = CODE_NEXT if compose else CODE
    trace = record["trace"][:-n]
    trace.append(8 << 8)
    if compose:
        trace.append(16 << 8)
    trace += [(9 << 8) | cpu for cpu in range(n)]
    trace.append((10 << 8) | (n - 1))
    for cpu in range(n):
        if compose:
            trace.append((17 << 8) | cpu)
        trace.append((6 << 8) | cpu)
        if cpu == n - 1:
            trace.append((14 << 8) | cpu)
        trace += [(event << 8) | cpu for event in (11, 15, 12)]
    record["trace"] = trace
    record["coherence"] = {"generation": 2, "acknowledgements": (1 << n) - 1,
                           "active": 0, "new_entries": (1 << n) - 1,
                           "new_completed": (1 << n) - 1, "new_references": 0,
                           "reclaimed": 1, "leaf": 0x101067}
    new_cells = []
    for cpu in range(n):
        old = record["cells"][cpu]
        initial = copy.deepcopy(old["captured"])
        initial["x"][16] = code
        captured = copy.deepcopy(initial)
        captured["x"][4] = 0xD00D987654321000 + cpu
        captured["x"][5] = captured["x"][20] = 0
        captured["x"][7] = captured["x"][26] = [1, 1, 1 << 63, 3][cpu]
        captured["x"][27] = [1, 1, 1 << 63, 3][cpu]
        captured["nzcv"] = [0x20000000, 0x20000000, 0xA0000000, 0x20000000][cpu]
        captured["q"][16] = ([(a | b) for a, b in zip(initial["q"][16], initial["q"][17])]
                               if compose else seed(cpu)[0]["q"][16])
        memory = replacement_seed(cpu)
        after = list(memory)
        after[128:144] = captured["q"][16]
        fault_frame = copy.deepcopy(captured)
        fault_frame["nzcv"] ^= 1 << 29
        new_cells.append({"id": cpu, "dirty_faults": 1,
                          "case": "live_or" if compose else "live_load", "status": 1,
                          "initial": initial, "captured": captured,
                          "memory_base": DATA + cpu * 256,
                          "memory_before": memory, "memory_after": after,
                          "bank_before": list(old["bank_before"]),
                          "bank_after": list(old["bank_before"]),
                          "object_pc": code, "capture_pc": old["capture_pc"],
                          "dirty_fault": {"frame": fault_frame, "esr": (0x25 << 26) | 64 | 15,
                                          "far": DATA + cpu * 256 + 128, "pc": code + 36,
                                          "spsr": fault_frame["nzcv"] | 0x3C4}})
    record["new_cells"] = new_cells
    if compose:
        record["source"] = {"old_eligible": 0, "published": 2, "stale_replies": 1,
                            "stale_entries": (1 << n) - 1, "reuse_blocked": (1 << n) - 1,
                            "bytes": list(X86_OR), "old_code": list(bytes(44) + RET),
                            "new_code": list(NATIVE_OR + RET)}
        record["requests"] = compiler_requests(0xABCDEF1234567890)
    return record


def known_events_result(n, delay):
    record = known_remap_result(n, delay, True)
    record["profile"] = "i4-events"
    trace = [(18 << 8) | cpu for cpu in range(n)]
    for word in record["trace"]:
        event, cpu = word >> 8, word & 255
        if event == 1 and cpu in (1, 3):
            if cpu == 1:
                trace.append((22 << 8) | cpu)
            trace.append((20 << 8) | cpu)
        trace.append(word)
        if event == 8:
            trace += [(19 << 8) | peer for peer in range(n)]
        if event == 12:
            trace.append((21 << 8) | cpu)
            if cpu in (0, 2):
                trace.append((20 << 8) | cpu)
    record["trace"] = trace
    record["events"] = [{"id": cpu, "pending": 0, "token_state": 2, "notifications": 2,
                         "consumed": 1, "deliveries": 1, "shadow": 0, "priority": 0,
                         "vector": 64 + cpu, "boundary": 3 if cpu in (1, 3) else 2,
                         "wait_checks": int(cpu == 0),
                         "blocked": [103, 8, 114, 0][cpu], "packet": event_packet(cpu)}
                        for cpu in range(n)]
    return record


def self_test():
    from check_i4_pressure import self_test as pressure_self_test
    pressure_self_test()
    for n in range(1, 5):
        check(known_result(n, 25), n, 25)
        check(known_fault_result(n, 25), n, 25)
        check(known_remap_result(n, 25), n, 25)
        check(known_remap_result(n, 25, True), n, 25)
        check(known_events_result(n, 25), n, 25)
    for cpu, flags in enumerate((0x40000000, 0x60000000, 0x90000000, 0)):
        equal(load_fault_frame(cpu, PRIVATE + 0x400)["nzcv"], flags,
              "known pre-CFINV carry representation")
    original = known_result(4, 25)
    mutations = [
        ("missing ready", lambda r: r.__setitem__("ready", 7)),
        ("extra finished", lambda r: r.__setitem__("finished", 31)),
        ("tested count", lambda r: r.__setitem__("tested_vcpus", 3)),
        ("configured count", lambda r: r.__setitem__("configured_vcpus", 3)),
        ("requested delay", lambda r: r.__setitem__("delayed_owner_ms", 0)),
        ("invalid delay", lambda r: r.__setitem__("delayed_owner_ms", -1)),
        ("duplicate CPU", lambda r: r["cells"][3].__setitem__("id", 2)),
        ("missing CPU", lambda r: r["cells"].pop()),
        ("cross-CPU data", lambda r: r["cells"][1].__setitem__("memory_after", r["cells"][0]["memory_after"])),
        ("cross-CPU bank", lambda r: r["cells"][1].__setitem__("bank_after", r["cells"][0]["bank_after"])),
        ("cross-CPU registers", lambda r: r["cells"][1].__setitem__("captured", r["cells"][0]["captured"])),
        ("bad initial and preserved state", lambda r: [r["cells"][2][key]["x"].__setitem__(19, 0) for key in ("initial", "captured")]),
        ("wrong data address", lambda r: r["cells"][2].__setitem__("memory_base", DATA)),
        ("wrong text", lambda r: r["cells"][2].__setitem__("capture_pc", PRIVATE - 4)),
        ("wrong body", lambda r: r["cells"][2].__setitem__("object_pc", CODE + 4)),
        ("lost vector root", lambda r: r["cells"][3]["captured"]["q"][17].__setitem__(15, 0)),
    ]
    for label, mutate in mutations:
        bad = copy.deepcopy(original)
        mutate(bad)
        try:
            check(bad, 4, 25)
        except (ValueError, KeyError, TypeError):
            continue
        raise ValueError("accepted corruption: " + label)
    original = known_fault_result(4, 25)
    fault_mutations = [
        ("missing fault", lambda r: r["faults"].pop()),
        ("double fault", lambda r: r["faults"][0].__setitem__("faults", 2)),
        ("lost old route", lambda r: r["faults"][1].__setitem__("old_gpa", DATA)),
        ("store fault substituted", lambda r: r["faults"][0].__setitem__("pc", CODE + 36)),
        ("false FAR", lambda r: r["faults"][0].__setitem__("esr", (0x25 << 26) | (1 << 10) | 7)),
        ("load already performed", lambda r: r["faults"][0]["frame"]["x"].__setitem__(4, 0xCAFE123456789000)),
        ("incorrect pre-CFINV", lambda r: r["faults"][1]["frame"].__setitem__("nzcv", 0x40000000)),
        ("lost references", lambda r: r.__setitem__("retained", 1)),
        ("early exclusive", lambda r: r["trace"].insert(4, 3 << 8)),
        ("non FIFO service", lambda r: r["trace"].__setitem__(10, (4 << 8) | 0)),
        ("overlapping service", lambda r: r["trace"].insert(11, (4 << 8) | 1)),
        ("early reference release", lambda r: r["trace"].insert(11, 6 << 8)),
        ("missing delay", lambda r: r["trace"].pop(7)),
        ("early delay", lambda r: r["trace"].insert(4, (7 << 8) | 3)),
        ("double completion", lambda r: r["trace"].insert(12, (5 << 8) | 2)),
        ("duplicate release", lambda r: r["trace"].append(6 << 8)),
        ("unknown event", lambda r: r["trace"].append(8 << 8)),
    ]
    for label, mutate in fault_mutations:
        bad = copy.deepcopy(original)
        mutate(bad)
        try:
            check(bad, 4, 25)
        except (ValueError, KeyError, TypeError):
            continue
        raise ValueError("accepted corruption: " + label)
    original = known_remap_result(4, 25)
    remap_mutations = [
        ("missing new execution", lambda r: r["new_cells"].pop()),
        ("wrong new identity", lambda r: r["new_cells"][1].__setitem__("id", 0)),
        ("false shadow fill", lambda r: r["coherence"].__setitem__("leaf", 0x100067)),
        ("missing dirty effect", lambda r: r["coherence"].__setitem__("leaf", 0x101027)),
        ("retained new frame", lambda r: r["coherence"].__setitem__("new_references", 1)),
        ("stale new load", lambda r: r["new_cells"][0]["captured"]["x"].__setitem__(4, 0xCAFE123456789000)),
        ("lost old effect", lambda r: r["cells"][0]["memory_after"].__setitem__(128, 0)),
        ("continuity lost", lambda r: [r["new_cells"][0][key]["x"].__setitem__(19, 0) for key in ("initial", "captured")]),
        ("dirty frame lost load", lambda r: r["new_cells"][1]["dirty_fault"]["frame"]["x"].__setitem__(4, 0)),
        ("wrong dirty carry", lambda r: r["new_cells"][2]["dirty_fault"]["frame"].__setitem__("nzcv", 0xA0000000)),
        ("wrong dirty site", lambda r: r["new_cells"][0]["dirty_fault"].__setitem__("pc", CODE + 16)),
        ("premature mutation", lambda r: r["trace"].insert(10, 8 << 8)),
        ("premature reopen", lambda r: r["trace"].insert(19, 10 << 8)),
        ("early reclamation", lambda r: r["trace"].insert(19, 14 << 8)),
        ("new entry before release", lambda r: r["trace"].insert(24, 11 << 8)),
        ("duplicated new completion", lambda r: r["trace"].append(12 << 8)),
    ]
    for label, mutate in remap_mutations:
        bad = copy.deepcopy(original)
        mutate(bad)
        try:
            check(bad, 4, 25)
        except (ValueError, KeyError, TypeError):
            continue
        raise ValueError("accepted corruption: " + label)
    original = known_remap_result(4, 25, True)
    compose_mutations = [
        ("source still XOR", lambda r: r["source"].__setitem__("bytes", list(X86_XOR))),
        ("stale source eligible", lambda r: r["source"].__setitem__("old_eligible", 1)),
        ("reply not rejected", lambda r: r["source"].__setitem__("stale_replies", 0)),
        ("missing reuse protection", lambda r: r["source"].__setitem__("reuse_blocked", 7)),
        ("retired code still live", lambda r: r["source"]["old_code"].__setitem__(0, 16)),
        ("wrong new native body", lambda r: r["source"]["new_code"].__setitem__(2, 0x31)),
        ("replacement request lost", lambda r: r["requests"].pop()),
        ("stale replacement identity", lambda r: r["requests"][2].__setitem__(2, 2)),
        ("wrong source generation", lambda r: r["requests"][2].__setitem__(3, 1)),
        ("wrong VM lifetime", lambda r: r["requests"][2].__setitem__(5, 1)),
        ("source packet mismatch", lambda r: r["requests"][2].__setitem__(7, r["requests"][0][7])),
        ("publication before mutation", lambda r: r["trace"].insert(10, 16 << 8)),
        ("no replacement publication", lambda r: r["trace"].remove(16 << 8)),
        ("no stale/reuse check", lambda r: r["trace"].remove(17 << 8)),
        ("old entry used", lambda r: r["new_cells"][0].__setitem__("object_pc", CODE)),
        ("wrong replacement instruction", lambda r: r["new_cells"][0].__setitem__("case", "live_load")),
    ]
    for label, mutate in compose_mutations:
        bad = copy.deepcopy(original)
        mutate(bad)
        try:
            check(bad, 4, 25)
        except (ValueError, KeyError, TypeError):
            continue
        raise ValueError("accepted corruption: " + label)
    original = known_events_result(4, 25)
    event_mutations = [
        ("missing event result", lambda r: r["events"].pop()),
        ("duplicate event CPU", lambda r: r["events"][1].__setitem__("id", 0)),
        ("pending request lost", lambda r: r["events"][0].__setitem__("consumed", 0)),
        ("duplicate delivery", lambda r: r["events"][0].__setitem__("deliveries", 2)),
        ("rearmed consumed request", lambda r: r["events"][1].__setitem__("token_state", 1)),
        ("missing renotification", lambda r: r["events"][3].__setitem__("notifications", 1)),
        ("no compiler wait recheck", lambda r: r["events"][0].__setitem__("wait_checks", 0)),
        ("missing IF gate", lambda r: r["events"][0].__setitem__("blocked", 99)),
        ("missing shadow gate", lambda r: r["events"][1].__setitem__("blocked", 0)),
        ("missing priority gate", lambda r: r["events"][2].__setitem__("blocked", 98)),
        ("wrong boundary", lambda r: r["events"][3].__setitem__("boundary", 1)),
        ("wrong vector", lambda r: r["events"][2].__setitem__("vector", 64)),
        ("stale canonical GPR", lambda r: r["events"][0]["packet"]["gpr"].__setitem__(0, 0)),
        ("stale arithmetic flags", lambda r: r["events"][2]["packet"].__setitem__("rflags", 0x202)),
        ("wrong initial RIP", lambda r: r["events"][3]["packet"].__setitem__("rip", DATA + 8192 + 17)),
        ("lost preserved upper YMM", lambda r: r["events"][0]["packet"]["ymm"][0].__setitem__(31, 0)),
        ("lost x87", lambda r: r["events"][0]["packet"]["x87_regs"][7].__setitem__(9, 0)),
        ("lost segment state", lambda r: r["events"][0]["packet"]["segments"][0].__setitem__(3, 0)),
        ("lost control state", lambda r: r["events"][0]["packet"]["control"].__setitem__(2, 0)),
        ("bool-as-integer packet", lambda r: r["events"][0]["packet"].__setitem__("reserved", False)),
        ("request unposted", lambda r: r["trace"].remove(18 << 8)),
        ("renotify outside compilation", lambda r: r["trace"].insert(0, 19 << 8)),
        ("initial eligible event skipped", lambda r: r["trace"].remove((20 << 8) | 3)),
        ("duplicate delivery trace", lambda r: r["trace"].append(20 << 8)),
        ("IF event delivered before completion", lambda r: r["trace"].insert(4, 20 << 8)),
        ("fixture shadow not cleared", lambda r: r["trace"].remove((22 << 8) | 1)),
        ("early fixture control clear", lambda r: r["trace"].insert(4, 21 << 8)),
    ]
    for label, mutate in event_mutations:
        bad = copy.deepcopy(original)
        mutate(bad)
        try:
            check(bad, 4, 25)
        except (ValueError, KeyError, TypeError):
            continue
        raise ValueError("accepted corruption: " + label)
    controls = len(event_mutations) + len(mutations) + len(fault_mutations) + len(remap_mutations) + len(compose_mutations)
    print(f"PASS: N=1/2/3/4 entry/fault/remap/compose/events answers; {controls} corruption controls")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--vcpus", type=int)
    parser.add_argument("--delay-ms", type=int)
    parser.add_argument("--profile", choices=("i4-entry", "i4-faults", "i4-remap", "i4-compose", "i4-events", "i4-pressure"))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if not args.paths and not args.self_test:
        parser.error("provide result JSON files or --self-test")
    if args.self_test:
        self_test()
    failures = 0
    for path in args.paths:
        try:
            check(json.loads(path.read_text()), args.vcpus, args.delay_ms, args.profile)
            print(f"PASS: {path}")
        except (OSError, ValueError, KeyError, TypeError) as error:
            failures += 1
            print(f"FAIL: {path}: {error}", file=sys.stderr)
    return int(failures != 0)


if __name__ == "__main__":
    sys.exit(main())

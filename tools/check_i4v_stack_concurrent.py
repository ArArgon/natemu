#!/usr/bin/env python3
"""Independent stack effects and ownership oracle for the finite N-vCPU slice.

The generated program pushes 8/2/2 bytes, then loads through the original RDX.
RSP remains continuous across old/new/ordinary executions and physical remaps.
No guest value or stack effect is normalized into the older scalar fixture.
"""

import argparse
import copy
import json
import math
from pathlib import Path

from check_i1 import resident_pc

import check_i4
from check_i3_paging import guest_seed as base_guest_seed, copy_live
from i4v_check import HIGH, IPA, PRIVATE, SOURCE, TCR, exact, require, uint
from i4v_stack_check import BODY as OLD_BODY, SOURCE_BYTES as OLD_SOURCE, NATIVE_OFFSETS, GUEST_OFFSETS
from check_i4v_alias import descriptor

DATA = 0x100000
CODE = PRIVATE + 0x30000
CODE_NEXT = CODE + 4096
NO_CPU = (1 << 64) - 1
NEW_SOURCE = OLD_SOURCE[:7] + b"\x53" + OLD_SOURCE[8:]
NEW_BODY = OLD_BODY[:12] + b"\x74" + OLD_BODY[13:]


def span(cpu, stack_profile=1):
    return (HIGH if stack_profile == 1 and cpu % 2 == 0 else 0) + DATA + cpu * 256


def seed(cpu, stack_profile=1):
    frame, bank, _ = check_i4.seed(cpu)
    frame["x"][6] = span(cpu, stack_profile)
    frame["x"][5] = frame["x"][8] = frame["x"][11] = span(cpu, stack_profile) + 0x80
    return frame, bank, [0xA5] * 256


def canonical_seed(cpu, rip, stack_profile=1):
    guest = base_guest_seed("cold")
    frame, _, _ = seed(cpu, stack_profile)
    copy_live(guest, frame)
    guest["gpr"][4] = frame["x"][8]
    guest["rip"] = rip
    if cpu == 0:
        guest["rflags"] &= ~0x200
    return guest


def code_image(replacement=False):
    body = NEW_BODY if replacement else OLD_BODY
    result = [0] * 320
    for step in range(5):
        instructions = body[NATIVE_OFFSETS[step]:NATIVE_OFFSETS[step + 1]] + bytes.fromhex("c0035fd6")
        result[step * 64:step * 64 + len(instructions)] = instructions
    return result


def execute(initial, before, cpu, code, capture, replacement, stack_profile=1):
    """Return five complete native frames and memory using literal x86 effects."""
    frame, memory, prefixes = copy.deepcopy(initial), list(before), []
    require(len(memory) == 256, "one owned physical span required")
    for step in range(5):
        frame["x"][16], frame["x"][30] = code + step * 64, capture
        if step < 3:
            width = (8, 2, 2)[step]
            value = (0xFFFFFFFFBEBDBCBB, 0x5153 if replacement else 0x5152, 0x6162)[step]
            offset = frame["x"][8] - width - span(cpu, stack_profile)
            require(0 <= offset <= 256 - width, "stack write escapes owner span")
            memory[offset:offset + width] = value.to_bytes(width, "little")
            frame["x"][8] -= width
            frame["x"][20] = value
        else:
            width, displacement, destination = (8, 8, 4) if step == 3 else (4, 12, 6)
            address = frame["x"][5] - displacement
            offset = address - span(cpu, stack_profile)
            require(0 <= offset <= 256 - width, "stack load escapes owner span")
            frame["x"][20] = address
            frame["x"][destination] = int.from_bytes(bytes(memory[offset:offset + width]), "little")
        prefixes.append(copy.deepcopy(frame))
    return prefixes, memory


def precommit(initial, prefixes, step, code, capture, replacement):
    frame = copy.deepcopy(initial if step == 0 else prefixes[step - 1])
    frame["x"][16], frame["x"][30] = code + step * 64, capture
    frame["x"][20] = (0xFFFFFFFFBEBDBCBB, 0x5153 if replacement else 0x5152, 0x6162)[step]
    return frame


def compact_prefixes(prefixes):
    return [[frame["x"][index] for index in (8, 4, 6, 20)] for frame in prefixes]


def canonical_after(cpu, rip, frame, cleared=True, stack_profile=1):
    guest = canonical_seed(cpu, rip, stack_profile)
    for guest_reg, native_reg in ((0, 4), (3, 6), (4, 8)):
        guest["gpr"][guest_reg] = frame["x"][native_reg]
    guest["rip"] = rip + 20
    if cleared:
        guest["rflags"] |= 0x200
    return guest


def check_schedule(record, n, visit_interval):
    """Replay FIFO ownership, independently of the resident reservation policy."""
    trace = record["trace"]
    require(type(trace) is list and 0 < len(trace) <= 512, "bounded ownership trace")
    for word in trace:
        uint(word, 64, "trace event")
    if record["stack_profile"] >= 2:
        begin = next((i for i, word in enumerate(trace) if word >> 8 == 32), None)
        require(begin is not None, "missing native denied-alias probes")
        tail, trace = trace[begin:], trace[:begin]
        require(len(tail) == 2 * n, "exactly one terminal probe per owner")
        begun, active, completed = set(), set(), set()
        for word in tail:
            uint(word, 64, "probe trace event")
            event, cpu = word >> 8, word & 255
            require(cpu < n, "probe owner outside configured set")
            if event == 32:
                require(cpu not in begun, "duplicate terminal probe grant")
                begun.add(cpu)
                active.add(cpu)
            elif event == 33:
                require(cpu in active, "probe completion without retained grant")
                active.remove(cpu)
                completed.add(cpu)
            else:
                raise ValueError("pressure mutation after terminal probes began")
        exact(completed, set(range(n)), "all native denied-alias probes completed")
        require(not active, "terminal native probe grant remains live")
    start = next((i for i, word in enumerate(trace) if word >> 8 >= 23), None)
    require(start is not None, "missing ordinary execution under maintenance")
    check_i4.check_fault_trace(trace[:start], n, remap=True, compose=True, events=True)
    m = record["maintenance"]
    intervals = m["intervals"]
    require(type(intervals) is list and 0 < len(intervals) <= 4 * n, "bounded ordinary intervals")
    ordered = {}
    for cell in intervals:
        ordinal = uint(cell["ordinal"], 64, "interval ordinal")
        require(1 <= ordinal <= len(intervals) and ordinal not in ordered, "unique interval ordinal")
        ordered[ordinal] = cell
    submitted, completed, counts = [0] * n, [0] * n, [0] * n
    pending, queue, acknowledgements, history, leaves = {}, [], set(), [], []
    current = ordinary = previous = None
    serial = total = batch = duplicates = depth = ordinary_total = 0
    closing = dirty = False
    for word in trace[start:]:
        event, cpu = word >> 8, word & 255
        require(cpu < n, "maintenance owner outside configured set")
        if event == 23:
            require(cpu not in pending and cpu != ordinary and submitted[cpu] < 9, "invalid pending operation")
            serial += 1
            submitted[cpu] += 1
            pending[cpu] = serial
            queue.append((cpu, serial))
            depth = max(depth, len(pending))
            require(len(pending) <= n, "pending capacity exceeded")
        elif event == 24:
            require(previous == (23, cpu) and cpu in pending, "duplicate must preserve submitted identity")
            duplicates += 1
        elif event == 25:
            require(current is None and ordinary is None and queue and batch < 8, "overlapping mutation or fairness violation")
            current = queue.pop(0)
            exact(cpu, current[0], "FIFO head owner")
            exact(pending[cpu], current[1], "protected pending identity")
            acknowledgements = set()
        elif event == 26:
            require(current is not None and ordinary is None and cpu not in acknowledgements, "invalid acknowledgement")
            if cpu == (3 + total) % n:
                exact(acknowledgements, set(range(n)) - {cpu}, "delayed acknowledgement must arrive last")
            acknowledgements.add(cpu)
        elif event == 27:
            require(current is not None and current[0] == cpu and ordinary is None, "completion without owned head")
            exact(acknowledgements, set(range(n)), "all owners acknowledge before completion")
            exact(pending.pop(cpu), current[1], "completion consumes original request")
            history.append(list(current))
            current = None
            completed[cpu] += 1
            total += 1
            batch += 1
            leaves.append((DATA + (4096 if total % 2 == 0 else 0)) | 0x27)
        elif event == 29:
            require(ordinary is None and current is None and cpu not in pending and 1 <= batch <= 8,
                    "ordinary grant overlaps pending owner or service")
            exact(cpu, ordinary_total % n, "round-robin ordinary owner")
            require(counts[cpu] < 4, "per-owner ordinary capacity exceeded")
            ordinary, closing, dirty = cpu, False, False
            require(ordinary_total + 1 in ordered, "executed ordinary interval missing from report")
            cell = ordered[ordinary_total + 1]
            for field, expected in (("id", cpu), ("heads", total), ("epoch", 2 + total)):
                exact(cell[field], expected, "ordinary " + field)
        elif event == 28:
            require(ordinary is not None and not closing and (n == 1 or cpu != ordinary), "missing peer closure")
            closing = True
        elif event == 31:
            require(ordinary == cpu and closing and not dirty and current is None, "dirty fault outside retained ordinary grant")
            dirty = True
        elif event == 30:
            require(ordinary == cpu and closing and dirty and current is None, "ordinary body incomplete")
            visit_interval(ordered[ordinary_total + 1], cpu, counts[cpu], int(total % 2 == 0))
            counts[cpu] += 1
            ordinary_total += 1
            ordinary, batch = None, 0
        else:
            raise ValueError(f"unknown maintenance event {event}")
        previous = (event, cpu)
    require(not pending and not queue and current is None and ordinary is None, "ownership remains live")
    exact(submitted, [9] * n, "nine submissions per owner")
    exact(completed, [9] * n, "nine completions per owner")
    require(ordinary_total >= math.ceil(total / 8) and all(counts), "ordinary work starved")
    exact(ordinary_total, len(intervals), "all ordinary executions accounted for")
    exact(depth, n, "pending storage saturation")
    exact(duplicates, serial, "one duplicate rejection per request")
    expected = {"ready": (1 << n) - 1, "serial": serial, "submitted": [9] * n + [0] * (4 - n),
                "completed": [9] * n + [0] * (4 - n), "pending": [0] * 4,
                "length": 0, "current": [NO_CPU, 0], "epoch": 2 + total, "total": total,
                "batch": 0, "next_ordinary": ordinary_total % n, "ordinary": NO_CPU,
                "ordinary_total": ordinary_total, "interval_counts": counts + [0] * (4 - n),
                "finished": 1, "duplicate_rejections": duplicates, "maximum_depth": n,
                "leaf_history": leaves, "leaf_final": leaves[-1] | 64,
                "request_history": history}
    exact({key: value for key, value in m.items() if key != "intervals"}, expected, "maintenance ownership")
    return counts


def request_packet(vm, rip, serial, chunk=0):
    variant = int(serial == 3)
    source = (NEW_SOURCE if variant else OLD_SOURCE) + bytes(4)
    words = [int.from_bytes(source[i:i + 8], "little") for i in (0, 8, 16)]
    return [0x49345611, 1, vm, 0, 1, 1 + variant, 1, rip, 64, 20, *words, serial, 52, chunk]


def reply_packet(vm, rip, serial, chunk):
    request = request_packet(vm, rip, serial, chunk)
    body = NEW_BODY if serial == 3 else OLD_BODY
    part = body[chunk * 24:(chunk + 1) * 24]
    padded = part + bytes(24 - len(part))
    words = [int.from_bytes(padded[i:i + 8], "little") for i in (0, 8, 16)]
    return [0x49345612, *request[1:9], 52, *words, serial, chunk, len(part)]


def check_program(cell, initial, before, cpu, replacement, capture, stack_profile=1):
    code = CODE_NEXT if replacement else CODE
    prefixes, after = execute(initial, before, cpu, code, capture, replacement, stack_profile)
    _, bank, _ = seed(cpu, stack_profile)
    for field, expected in (("id", cpu), ("case", "fex_push_immediates64_changed" if replacement else "fex_push_immediates64"),
                            ("status", 1), ("initial", initial), ("captured", prefixes[-1]),
                            ("memory_base", span(cpu, stack_profile)), ("memory_before", before), ("memory_after", after),
                            ("bank_before", bank), ("bank_after", bank), ("object_pc", code), ("capture_pc", capture)):
        exact(cell[field], expected, f"CPU{cpu} program." + field)
    if replacement:
        exact(cell["dirty_faults"], 1, "one first-push dirty fault")
        check_fault(cell["dirty_fault"], precommit(initial, prefixes, 0, code, capture, True),
                    initial["x"][8] - 8, code + 8, permission=True)
    return prefixes, after


def check_fault(fault, frame, address, pc, permission):
    exact(fault["frame"], frame, "fault full native prefix; failed preindex must not update RSP")
    exact(fault["far"], address, "native stack FAR")
    exact(fault["pc"], pc, "generated preindexed store site")
    esr = uint(fault["esr"], 64, "native ESR")
    exact(esr >> 26, 0x25, "same-EL data abort")
    exact((esr >> 6) & 1, 1, "stack write fault")
    require(esr & 63 in (range(12, 16) if permission else range(4, 8)), "incorrect native fault class")
    exact(esr & ((1 << 10) | (1 << 8) | (1 << 7)), 0, "direct valid stack FAR")
    spsr = uint(fault["spsr"], 64, "native SPSR")
    exact(spsr & 0xF0000000, frame["nzcv"], "flags preserved at native fault")
    exact(spsr & 0x3DF, 0x3C4, "masked EL1t")


def check_events(record, n, rip, second_frames, stack_profile=1):
    events = record["events"]
    require(type(events) is list and len(events) == n, "one event record per owner")
    seen = set()
    for event in events:
        cpu = uint(event["id"], 32, "event owner")
        require(cpu < n and cpu not in seen, "invalid event participant")
        seen.add(cpu)
        initial = cpu in (1, 3)
        for field, expected in (("pending", 0), ("token_state", 2), ("notifications", 2),
                                ("consumed", 1), ("deliveries", 1), ("shadow", 0), ("priority", 0),
                                ("vector", 64 + cpu), ("boundary", 3 if initial else 2)):
            exact(event[field], expected, "event." + field)
        waits = uint(event["wait_checks"], 64, "event wait checks")
        if cpu == 0:
            require(waits > 0, "compiler owner did not recheck pending events")
        exact(event["blocked"], [2 | 4 | 32 | 64, 8, 2 | 16 | 32 | 64, 0][cpu] | int(waits > 0),
              "exact event blockers")
        expected = canonical_seed(cpu, rip, stack_profile) if initial else canonical_after(cpu, rip, second_frames[cpu], stack_profile=stack_profile)
        exact(event["packet"], expected, "canonical stack event packet")


def check_aliases(record, n):
    aliases = record["aliases"]
    require(type(aliases) is list and len(aliases) == n, "one root/alias snapshot per owner")
    profile = record["alias_profile"]
    stack_profile = record["stack_profile"]
    pair = [[descriptor(0), descriptor(0)] for _ in range(n)]
    acknowledgements = [[] for _ in range(n)]
    total = 0
    for word in record["trace"]:
        event, cpu = word >> 8, word & 255
        if event in (9, 26):
            epoch = 2 if event == 9 else 3 + total
            route = descriptor(1 if event == 9 else int((total + 1) % 2 == 0))
            pair[cpu] = [route, route]
            acknowledgements[cpu].append([epoch, route, route])
        elif event in (15, 31):
            pair[cpu][int(stack_profile == 1 and cpu % 2 == 0)] = descriptor(1 if event == 15 else int(total % 2 == 0), False)
        elif event == 27:
            total += 1
        elif event == 32:
            pair[cpu] = [descriptor(int(total % 2 == 0), False), descriptor(int(total % 2 == 0))]
    seen = set()
    for a in aliases:
        cpu = uint(a["id"], 32, "alias owner")
        require(cpu < n and cpu not in seen, "invalid alias owner")
        seen.add(cpu)
        require(uint(a["sctlr"], 64, "SCTLR") & 0x1005 == 0x1005, "MMU/cache profile disabled")
        exact((a["sctlr"] >> 1) & 1, 0, "alignment checking must allow the fixture's unaligned pushes")
        require(uint(a["actlr"], 64, "ACTLR") & 2 == 2, "TSO profile disabled")
        root, l1, l2, lower, upper = ([0] * 512 for _ in range(5))
        base = IPA + cpu * 0x100000
        root[256], l1[0], l2[0] = [base + offset + 3 for offset in (0x18000, 0x19000, 0x1A000)]
        lower[256], upper[257 if stack_profile == 3 else 256] = pair[cpu]
        writer = cpu == 0
        if writer:
            (upper if profile == 1 and stack_profile != 2 else lower)[258] = descriptor(2, False)
        acks = acknowledgements[cpu]
        exact(len(acks), 1 + 9 * n, "all paired-route acknowledgements")
        warm = [0, 0]
        warm[0 if stack_profile == 1 and cpu % 2 == 0 else 1] = descriptor(0)
        expected = {"id": cpu, "current_el": 4, "tcr": TCR, "ttbr1": base + 0x17000,
                    "warm": warm, "ack_count": len(acks),
                    "acks": acks + [[0] * 3 for _ in range(37 - len(acks))],
                    "wrong_rip_rejections": 0, "blank_before_publish": 0,
                    "source_address": SOURCE + (HIGH if profile == 1 and stack_profile != 2 else 0) if writer else 0,
                    "source_gpa": SOURCE if writer else 0,
                    "source_leaf_before": SOURCE | 0x27 if writer else 0,
                    "source_leaf_after": SOURCE | 0x67 if writer else 0,
                    "source_before": list(OLD_SOURCE[:17]) if writer else [0] * 17,
                    "source_after": list(NEW_SOURCE[:17]) if writer else [0] * 17,
                    "upper_root": root, "upper_l1": l1, "upper_l2": l2,
                    "upper_leaf": upper, "lower_leaf": lower}
        exact({k: v for k, v in a.items() if k not in ("sctlr", "actlr")}, expected, "stack alias mappings")


def zeros(value):
    """Zero-valued ABI transport, preserving every field and array dimension."""
    if isinstance(value, dict):
        return {key: zeros(item) for key, item in value.items()}
    if isinstance(value, list):
        return [zeros(item) for item in value]
    return 0


def probe_expectation(cpu, rip, frame, stack_profile, page, capture):
    # Explicit fixture rebinding precedes the actual instruction. The failed
    # PUSH may materialize its scratch immediate, but cannot commit guest RSP.
    initial = copy.deepcopy(frame)
    initial["x"][8] += HIGH + (4096 if stack_profile == 3 else 0)
    initial["x"][16], initial["x"][30] = CODE_NEXT, capture
    captured = copy.deepcopy(initial)
    captured["x"][20] = 0xFFFFFFFFBEBDBCBB
    before = canonical_after(cpu, rip, frame, stack_profile=stack_profile)
    after = copy.deepcopy(before)
    after["gpr"][4], after["rip"] = initial["x"][8], rip
    address = initial["x"][8] - 8
    after["control"][1] = address
    gpa = DATA + page * 4096
    tables = [0x2027, 0x2025 if stack_profile == 2 else 0x2027, gpa | 0x67,
              gpa | 0x25 if stack_profile == 3 else (DATA + 4096) | 7]
    return {"initial": initial, "captured": captured, "guest_before": before, "guest_after": after,
            "pair": [descriptor(page, False), descriptor(page)], "tables_before": tables,
            "tables_after": list(tables), "vector": 14, "error": 3, "address": address,
            "rip": rip, "far": address, "pc": CODE_NEXT + 8, "done": 1}


def check_probe(probe, cpu, rip, frame, stack_profile, page, capture):
    expected = probe_expectation(cpu, rip, frame, stack_profile, page, capture)
    if stack_profile == 1:
        expected.update(esr=0, spsr=0)
        exact(probe, zeros(expected), "unused permission probe must remain zero")
        return
    exact({k: v for k, v in probe.items() if k not in ("esr", "spsr")}, expected,
          "denied alias PUSH preserves every committed prefix and memory effect")
    check_fault({"frame": probe["captured"], **{k: probe[k] for k in ("esr", "spsr", "far", "pc")}},
                expected["captured"], expected["address"], CODE_NEXT + 8, permission=True)


def owners(items, n, label):
    require(type(items) is list and len(items) == n, label + ": one record per owner")
    result = {}
    for item in items:
        cpu = uint(item["id"], 32, label + " id")
        require(cpu < n and cpu not in result, label + ": invalid or duplicate owner")
        result[cpu] = item
    return result


def check(record, expected_vcpus=None, expected_alias_profile=None, expected_stack_profile=None):
    require(type(record) is dict, "expected concurrent stack report")
    n = uint(record["configured_vcpus"], 32, "configured_vcpus")
    require(1 <= n <= 4, "finite participant range is1..4")
    profile = uint(record["alias_profile"], 32, "alias_profile")
    require(profile in (1, 2), "source address profile must be1or2")
    if expected_vcpus is not None:
        exact(n, expected_vcpus, "requested participant count")
    if expected_alias_profile is not None:
        exact(profile, expected_alias_profile, "requested source regime")
    stack_profile = uint(record["stack_profile"], 32, "stack_profile")
    require(stack_profile in (1, 2, 3), "known stack permission profile")
    if expected_stack_profile is not None:
        exact(stack_profile, expected_stack_profile, "requested stack permission profile")
    name = {1: "stack", 2: "stack-parent", 3: "stack-leaf"}[stack_profile]
    rip = SOURCE + (HIGH if profile == 2 else 0)
    for field, expected in (("profile", "i4-" + name + ("-low" if profile == 1 else "-high")),
                            ("stack_profile", stack_profile), ("source_rip", rip), ("tested_vcpus", n),
                            ("delayed_owner_ms", 50), ("ready", (1 << n) - 1),
                            ("finished", (1 << n) - 1), ("retained", 0)):
        exact(record[field], expected, field)
    old = owners(record["cells"], n, "old programs")
    new = owners(record["new_cells"], n, "new programs")
    faults = owners(record["faults"], n, "retained old faults")
    stacks = owners(record["stacks"], n, "stack records")
    frames, second_frames, programs, memory, captures = {}, {}, {}, {}, {}
    for cpu in range(n):
        initial, _, before = seed(cpu, stack_profile)
        capture = uint(old[cpu]["capture_pc"], 64, "capture PC")
        require(resident_pc(capture), "capture outside resident text")
        captures[cpu] = capture
        prefixes, after = check_program(old[cpu], initial, before, cpu, False, capture, stack_profile)
        exact(faults[cpu]["faults"], 1, "one queued second-push fault")
        exact(faults[cpu]["old_gpa"], DATA + cpu * 256, "retained old physical span")
        check_fault(faults[cpu], precommit(initial, prefixes, 1, CODE, capture, False),
                    initial["x"][8] - 10, CODE + 68, permission=False)
        memory[(0, cpu)] = after
        programs[cpu] = [{"entries": 31, "completed": 31, "prefixes": compact_prefixes(prefixes)}]
        next_initial = copy.deepcopy(prefixes[-1])
        next_initial["x"][16] = CODE_NEXT
        prefixes, after = check_program(new[cpu], next_initial, before, cpu, True, capture, stack_profile)
        memory[(1, cpu)] = after
        programs[cpu].append({"entries": 31, "completed": 31, "prefixes": compact_prefixes(prefixes)})
        frames[cpu] = copy.deepcopy(prefixes[-1])
        second_frames[cpu] = copy.deepcopy(prefixes[-1])

    def ordinary(cell, cpu, index, page):
        exact(cell["gpa"], DATA + page * 4096 + cpu * 256, "ordinary physical route")
        initial = copy.deepcopy(frames[cpu])
        initial["x"][16] = CODE_NEXT
        prefixes, after = check_program(cell, initial, memory[(page, cpu)], cpu, True, captures[cpu], stack_profile)
        memory[(page, cpu)], frames[cpu] = after, copy.deepcopy(prefixes[-1])
        exact(len(programs[cpu]), index + 2, "continuous per-owner program sequence")
        programs[cpu].append({"entries": 31, "completed": 31, "prefixes": compact_prefixes(prefixes)})

    counts = check_schedule(record, n, ordinary)
    check_events(record, n, rip, second_frames, stack_profile)
    check_aliases(record, n)
    exact(record["coherence"], {"generation": 2, "acknowledgements": (1 << n) - 1,
                               "active": 0, "new_entries": (1 << n) - 1, "new_completed": (1 << n) - 1,
                               "new_references": 0, "reclaimed": 1, "leaf": (DATA + 4096) | 0x67},
          "composed mapping and lifetime closure")
    requests = record["requests"]
    require(type(requests) is list and len(requests) == 3, "three actual FEX compilations")
    require(type(requests[0]) is list and len(requests[0]) == 16, "compile packet shape")
    vm = uint(requests[0][2], 64, "compiler VM nonce")
    require(vm != 0, "zero VM nonce")
    expected_requests = [request_packet(vm, rip, serial) for serial in range(1, 4)]
    exact(requests, expected_requests, "actual source/owner/incarnation/generation/RIP compile identities")
    replies = [[reply_packet(vm, rip, serial, chunk) for chunk in range(3)] for serial in range(1, 4)]
    exact(record["source"], {"old_eligible": 0, "published": 2, "stale_replies": 1,
                            "stale_entries": (1 << n) - 1, "reuse_blocked": (1 << n) - 1,
                            "bytes": list(NEW_SOURCE), "old_code": [0] * 320, "new_code": code_image(True)},
          "actual stack source replacement and old-code retirement")
    for cpu in range(n):
        compiler = cpu == 0
        expected_programs = programs[cpu] + [{"entries": 0, "completed": 0, "prefixes": [[0] * 4 for _ in range(5)]}
                                          for _ in range(6 - len(programs[cpu]))]
        expected = {"id": cpu, "step": 4, "pass": counts[cpu] + 1, "code": CODE_NEXT,
                    "expected": request_packet(vm, rip, 3, 2) if compiler else [0] * 16,
                    "compile_requests": expected_requests if compiler else [[0] * 16 for _ in range(3)],
                    "replies": replies if compiler else [[[0] * 16 for _ in range(3)] for _ in range(3)],
                    "programs": expected_programs, "stale_chunks": 3 if compiler else 0,
                    "wrong_owner_rejections": int(compiler), "blank_before_publish": int(compiler),
                    "source_before": list(OLD_SOURCE) if compiler else [0] * 20,
                    "source_after": list(NEW_SOURCE) if compiler else [0] * 20,
                    "guest_before": canonical_seed(cpu, rip, stack_profile), "guest_after": canonical_after(cpu, rip, frames[cpu], stack_profile=stack_profile)}
        if stack_profile >= 2:
            expected["guest_after"] = probe_expectation(cpu, rip, frames[cpu], stack_profile,
                int(record["maintenance"]["total"] % 2 == 0), captures[cpu])["guest_after"]
        exact({k: v for k, v in stacks[cpu].items() if k != "probe"}, expected, f"CPU{cpu} precise stack progress and publication evidence")
        if stack_profile >= 2 or "probe" in stacks[cpu]:
            check_probe(stacks[cpu]["probe"], cpu, rip, frames[cpu], stack_profile,
                        int(record["maintenance"]["total"] % 2 == 0), captures[cpu])
        exact(frames[cpu]["x"][8], span(cpu, stack_profile) + 128 - 12 * len(programs[cpu]), "continuous RSP across every remap")
    physical = [0xA5] * 8192
    for (page, cpu), contents in memory.items():
        offset = page * 4096 + cpu * 256
        physical[offset:offset + 256] = contents
    exact(record["stack_memory_before"], [0xA5] * 8192, "entire initial physical pages")
    exact(record["stack_memory_after"], physical, "all physical stack effects and unowned-byte guards")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--vcpus", type=int)
    parser.add_argument("--stack-profile", type=int, choices=(1, 2, 3))
    parser.add_argument("--alias-profile", type=int, choices=(1, 2))
    args = parser.parse_args()
    for path in args.paths:
        try:
            check(json.loads(path.read_text()), args.vcpus, args.alias_profile, args.stack_profile)
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise SystemExit(f"{path}: FAIL: {error}") from error
        print(f"{path}: PASS (finite N-vCPU stack pressure only)")


if __name__ == "__main__":
    main()

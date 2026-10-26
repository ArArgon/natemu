#!/usr/bin/env python3
"""Independent terminal-stack and checked split-store expectations.

The ordinary pressure prefix is checked by its existing oracle. Boundary effects
start from that validated prefix, never from reset memory or reported results.
"""

import argparse
import copy
import json
from pathlib import Path

import check_i4v_stack_concurrent as baseline
from check_i1 import resident_pc
from check_i4v_alias import descriptor
from i4v_check import HIGH, PRIVATE, exact, require, uint, zero_frame


OFFSETS = (0, 5, 9, 13, 17, 20)
WIDTHS = (8, 2, 2, 8, 4)
PUSH_VALUES = (0xFFFFFFFFBEBDBCBB, 0x5153, 0x6162)
SCENARIOS = ("ss_after_first", "gp_after_pushes", "split", "split_np", "split_ro")
MASK64 = (1 << 64) - 1
DATA = 0x100000
BOUNDARY = HIGH + DATA + 0x5000
OUTCOMES = ("ss", "gp", "split", "split-np", "split-ro")


def check_envelope(record, expected_profile=None, expected_vcpus=None,
                   expected_actor=None, expected_region=None):
    require(type(record) is dict, "expected boundary report")
    exact(record["case"], "stack-boundary", "boundary report kind")
    n = uint(record["configured_vcpus"], 32, "configured participants")
    profile = uint(record["profile"], 32, "boundary profile")
    actor = uint(record["actor"], 32, "boundary actor")
    require(1 <= n <= 4 and 1 <= profile <= 5 and actor < n, "bounded boundary matrix")
    exact(record["tested_vcpus"], n, "all configured owners executed")
    if "n" in record:
        exact(record["n"], n, "wrapper participant identity")
    exact(record["outcome"], OUTCOMES[profile - 1], "declared outcome")
    region = "high" if (profile + actor) % 2 else "low"
    exact(record["region"], region, "matrix source-address selection")
    for actual, expected, name in ((profile, expected_profile, "profile"), (n, expected_vcpus, "vCPUs"),
                                   (actor, expected_actor, "actor"), (region, expected_region, "region")):
        if expected is not None:
            exact(actual, expected, "requested " + name)
    shared = record["boundary"]
    for name, expected in (("n", n), ("profile", profile), ("actor", actor)):
        exact(shared[name], expected, "resident boundary " + name)
    owners = baseline.owners(record["owners"], n, "boundary owners")
    check_prefix(record["prefix"], shared, owners, n, 2 if region == "high" else 1)
    exact(shared["code_before"], baseline.code_image(True), "actual replacement code before terminal wave")
    exact(shared["code_before"], record["prefix"]["source"]["new_code"], "captured prefix code")
    return n, profile, actor, region, shared, owners


def prefix_tables(n):
    """Both aliases traversed; source rewritten once; last ordinary push dirties data."""
    page = DATA + (4096 if n % 2 == 0 else 0)
    words = {0: 0x2027, 256 * 8: 0x2027, 0x1000: 0x3027,
             0x2000: 0x4027, 0x3800: page | 0x67,
             0x3808: (DATA + 4096) | 7, 0x3810: 0x102067}
    result = [0] * 16384
    for offset, word in words.items():
        result[offset:offset + 8] = word.to_bytes(8, "little")
    return result


def check_prefix(record, shared, owners, n, region):
    """Bind captured boundary inputs to a complete independent pressure replay."""
    baseline.check(record, n, region, 1, before_wave=True)
    exact(shared["data_before"], record["stack_memory_after"], "pre-wave memory is the pressure result")
    exact(shared["tables_before"], prefix_tables(n), "pre-wave guest tables and A/D")
    exact(shared["finished_before"], 0, "no owner reported stopped before boundary wave")
    exact(shared["trace_start"], len(record["trace"]), "captured pressure trace boundary")
    stacks = baseline.owners(record["stacks"], n, "prefix guest state")
    aliases = baseline.owners(record["aliases"], n, "prefix native page tables")
    frames = {cell["id"]: cell["captured"] for cell in record["new_cells"]}
    for cell in sorted(record["maintenance"]["intervals"], key=lambda c: c["ordinal"]):
        frames[cell["id"]] = cell["captured"]
    for cpu in range(n):
        owner = owners[cpu]
        exact(owner["before_frame"], frames[cpu], f"owner {cpu} actual prefix frame")
        exact(owner["before_guest"], stacks[cpu]["guest_after"], f"owner {cpu} actual prefix guest")
        exact(owner["lower_before"], aliases[cpu]["lower_leaf"], f"owner {cpu} actual lower mappings")
        exact(owner["upper_before"], aliases[cpu]["upper_leaf"], f"owner {cpu} actual upper mappings")


def canonical_span(address, width):
    """48-bit canonical spans; reject overflow and the intervening hole."""
    end = address + width - 1
    return (0 <= address <= end < 1 << 64 and
            (end < 1 << 47 or address >= HIGH))


def access(frame, step):
    require(type(step) is int and 0 <= step < 5, "instruction index")
    width = WIDTHS[step]
    address = (frame["x"][8] - width if step < 3 else
               frame["x"][5] - (8 if step == 3 else 12)) & MASK64
    return address, width


def fragments(address, width):
    require(canonical_span(address, width), "complete access must be canonical")
    first = min(width, 4096 - (address & 4095))
    return [(address, first)] + ([(address + first, width - first)] if first < width else [])


def physical_bytes(address, width, pages):
    """Translate through an independently constructed, pinned page map."""
    result = []
    for linear, size in fragments(address, width):
        page = linear & ~4095
        require(page in pages, "missing admitted physical route")
        require(pages[page] & 4095 == 0, "unaligned physical route")
        result.extend(pages[page] + (linear & 4095) + offset for offset in range(size))
    return result


def instruction(initial, memory, pages, step, code, capture):
    """One successful actual source-variant1 instruction, with native scratch."""
    frame, after = copy.deepcopy(initial), dict(memory)
    address, width = access(frame, step)
    physical = physical_bytes(address, width, pages)
    require(all(p in memory for p in physical), "access escapes owned memory")
    frame["x"][16], frame["x"][30] = code + step * 64, capture
    if step < 3:
        value = PUSH_VALUES[step]
        for p, byte in zip(physical, value.to_bytes(width, "little")):
            after[p] = byte
        frame["x"][8] = address
        frame["x"][20] = value
    else:
        frame["x"][20] = address
        frame["x"][4 if step == 3 else 6] = int.from_bytes(bytes(memory[p] for p in physical), "little")
    return frame, after


def prefix(initial, memory, pages, completed, code, capture):
    require(type(completed) is int and 0 <= completed <= 5, "committed instruction prefix")
    frame, after, frames = copy.deepcopy(initial), dict(memory), []
    for step in range(completed):
        frame, after = instruction(frame, after, pages, step, code, capture)
        frames.append(copy.deepcopy(frame))
    return frame, after, frames


def native_fault_frame(initial, step, code, capture):
    """Preindexed ARM store abort: preceding immediate materializes, RSP does not."""
    require(type(step) is int and 0 <= step < 3, "supported generated store site")
    frame = copy.deepcopy(initial)
    frame["x"][16], frame["x"][30] = code + step * 64, capture
    frame["x"][20] = PUSH_VALUES[step]
    return frame


def canonical_after(before, frame, rip, completed, vector=0, fault_address=0):
    guest = copy.deepcopy(before)
    for gpr, native in ((0, 4), (3, 6), (4, 8)):
        guest["gpr"][gpr] = frame["x"][native]
    guest["rip"] = rip + OFFSETS[completed]
    if vector == 14:
        guest["control"][1] = fault_address
    return guest


def outcome(scenario, split_boundary):
    require(scenario in SCENARIOS, "unknown boundary scenario")
    completed, vector, error = {
        "ss_after_first": (1, 12, 0), "gp_after_pushes": (3, 13, 0),
        "split": (5, 0, 0), "split_np": (0, 14, 2), "split_ro": (0, 14, 3),
    }[scenario]
    address = (HIGH - 2 if vector == 12 else PRIVATE if vector == 13 else
               split_boundary if vector == 14 else 0)
    return completed, vector, error, address


def split_leaf_effects(first, second, permitted):
    """Selected two-phase write readiness: A may precede outcome; new D may not.

    Leaves here are independent fixture inputs, not values copied from a report.
    This models the scoped supervisor WP=1 profile and its first-then-second
    readiness order; ancestor A effects are handled by the complete table replay.
    """
    require(first & 3 == 3, "first fragment must permit supervisor write")
    a = first | 32
    b = second | 32 if second & 3 == 3 else second
    if permitted:
        require(second & 3 == 3, "second fragment must permit supervisor write")
        a |= 64
        b |= 64
    else:
        require(second & 3 != 3, "denied second fragment expected")
    return a, b


def table_word(tables, gpa, value=None):
    require(type(gpa) is int and gpa % 8 == 0 and 0x1000 <= gpa <= 0x4FF8,
            "fixture table access outside owned four-level tables")
    offset = gpa - 0x1000
    if value is not None:
        tables[offset:offset + 8] = value.to_bytes(8, "little")
    return int.from_bytes(bytes(tables[offset:offset + 8]), "little")


def walk(tables, address, write=False, defer_dirty=False):
    """Independent scoped supervisor/WP=1 four-level access and its A/D effects."""
    require(canonical_span(address, 1), "walk requires canonical address")
    pa, writable = 0x1000, True
    for shift in (39, 30, 21, 12):
        at = pa + ((address >> shift) & 511) * 8
        value = table_word(tables, at)
        if not value & 1:
            return {"fault": 2 if write else 0, "address": address}
        writable = writable and bool(value & 2)
        if shift == 12:
            if write and not writable:
                return {"fault": 3, "address": address}
            updated = value | 32 | (64 if write and not defer_dirty else 0)
            table_word(tables, at, updated)
            return {"gpa": (value & 0x000FFFFFFFFFF000) + (address & 4095),
                    "leaf": at, "before": value, "after": updated, "writable": int(writable)}
        require(not value & 128, "large pages are not part of this boundary fixture")
        table_word(tables, at, value | 32)
        pa = value & 0x000FFFFFFFFFF000
    raise AssertionError("unreachable")


def split_readiness(tables, address):
    """Probe both write fragments; only complete readiness permits new dirty bits."""
    after, routes = list(tables), []
    for linear, width in fragments(address, 8):
        route = walk(after, linear, write=True, defer_dirty=True)
        routes.append((linear, width, route))
        if "fault" in route:
            return after, routes, route["fault"]
    require(len(routes) == 2, "checked boundary store must split into two fragments")
    for _, _, route in routes:
        table_word(after, route["leaf"], table_word(after, route["leaf"]) | 64)
    return after, routes, 0


def check_effects(record, envelope):
    """Derive all wave state from the checked prefix, with disjoint owner effects."""
    n, profile, actor, region, shared, owners = envelope
    scenario = SCENARIOS[profile - 1]
    rip = 0x102000 + (HIGH if region == "high" else 0)
    old_page = DATA + (4096 if n % 2 == 0 else 0)
    other_page = DATA + 4096 - (old_page - DATA)
    exact(shared["old_page"], old_page, "retained pre-wave physical page")
    exact(shared["other_page"], other_page, "other owned physical page")
    configured = [other_page | 7 if profile == 1 else 0,
                  other_page | 7 if profile >= 3 else 0,
                  (0 if profile == 4 else old_page | (5 if profile == 5 else 7)) if profile >= 3 else 0]
    exact(shared["configured_leaves"], configured, "fresh boundary mappings")
    tables = list(shared["tables_before"])
    if profile == 1:
        table_word(tables, 0x4000, configured[0])
    if profile >= 3:
        table_word(tables, 0x4820, configured[1])
        table_word(tables, 0x4828, configured[2])
    before = dict(enumerate(shared["data_before"], DATA))
    memory, touched = dict(before), set()
    pages = {DATA: old_page, HIGH + DATA: old_page}
    if profile == 1:
        pages[HIGH] = other_page
    if profile >= 3:
        pages[BOUNDARY - 4096], pages[BOUNDARY] = other_page, old_page
    all_bits = (1 << n) - 1
    expected_owners = []
    captures = set()
    for cpu in range(n):
        o = owners[cpu]
        capture = uint(o["capture_pc"], 64, "boundary capture PC")
        require(resident_pc(capture), "boundary continuation outside executable resident text")
        captures.add(capture)
        initial = copy.deepcopy(o["before_frame"])
        if cpu == actor:
            if profile == 1:
                initial["x"][8] = initial["x"][5] = HIGH + 8
            elif profile == 2:
                initial["x"][5] = PRIVATE + 8
            else:
                initial["x"][8], initial["x"][5] = BOUNDARY + 4, BOUNDARY + 8
        initial["x"][16], initial["x"][30] = baseline.CODE_NEXT, capture
        guest_initial = copy.deepcopy(o["before_guest"])
        guest_initial["gpr"][2], guest_initial["gpr"][4] = initial["x"][5], initial["x"][8]
        guest_initial["rip"] = rip
        exact(o["initial"], initial, f"owner {cpu} explicit fixture binding")
        exact(o["initial_guest"], guest_initial, f"owner {cpu} canonical fixture binding")
        split_actor = cpu == actor and profile >= 3
        if cpu == actor:
            completed, vector, error, address = outcome(scenario, BOUNDARY)
        else:
            completed, vector, error, address = 5, 0, 0, 0
        # All owner write spans must be disjoint; this allows replay independent
        # of incidental native instruction interleavings before peer detachment.
        f, local, prefixes = prefix(initial, before, pages, completed, baseline.CODE_NEXT, capture)
        writes, cursor = set(), initial["x"][8]
        for step in range(min(completed, 3)):
            cursor -= WIDTHS[step]
            writes.update(physical_bytes(cursor, WIDTHS[step], pages))
        require(not touched.intersection(writes), "wave owners overlap physical write spans")
        touched.update(writes)
        for p in writes:
            memory[p] = local[p]
        fault_step = 0 if split_actor else 1
        faulted = split_actor or cpu != actor
        fault_initial = initial if split_actor else prefixes[0] if faulted else None
        fault_frame = native_fault_frame(fault_initial, fault_step, baseline.CODE_NEXT, capture) if faulted else zero_frame()
        if faulted and completed == 0:
            f = copy.deepcopy(fault_frame)
        entered = copy.deepcopy(initial if completed <= 1 else prefixes[completed - 2])
        last_entry = completed - 1 if completed else 0
        entered["x"][16], entered["x"][30] = baseline.CODE_NEXT + last_entry * 64, capture
        guest = canonical_after(guest_initial, f, rip, completed, vector, address)
        expected = {"current": f, "prefixes": prefixes + [zero_frame() for _ in range(5 - completed)],
                    "entries": (1 << completed) - 1 if completed else 1,
                    "completed": (1 << completed) - 1, "step": min(completed, 4),
                    "fault_frame": fault_frame, "faults": int(faulted), "guest_after": guest,
                    "vector": vector, "error": error, "address": address,
                    "rip": rip + OFFSETS[completed] if vector else 0,
                    "saved_page": other_page if cpu == actor and profile != 2 else old_page,
                    "saved_writable": 1,
                    "native_pair_before": [0, 0] if split_actor else
                        [descriptor((other_page if cpu == actor and profile == 1 else old_page) // 4096 - DATA // 4096, False), 0],
                    "native_pair_after": [descriptor((other_page - DATA) // 4096, False), descriptor((old_page - DATA) // 4096, False)]
                        if split_actor and profile == 3 else [0, 0],
                    "service_attempts": int(split_actor), "service_completed": int(split_actor and profile == 3),
                    "service_pc": baseline.CODE_NEXT + 8 if split_actor else 0,
                    "service_resume_pc": baseline.CODE_NEXT + 12 if split_actor and profile == 3 else 0,
                    "ready_mask": (3 if profile == 3 else 1) if split_actor else 0,
                    "retained_at_outcome": all_bits if cpu == actor else 0,
                    "ack_pair": [descriptor((other_page - DATA) // 4096)] * 2, "released": 1,
                    "final_frame": f, "final_guest": guest, "entered": entered,
                    "status": 1, "detail": 0}
        if faulted:
            esr = uint(o["esr"], 64, "native data abort ESR")
            require(esr >> 26 == 0x25 and esr & 64 and 4 <= esr & 63 <= 7,
                    "expected real native store translation fault")
            require(esr & ((1 << 7) | (1 << 8) | (1 << 10)) == 0,
                    "direct generated store requires valid FAR, not table-walk/cache-maintenance fault")
            due, width = access(fault_initial, fault_step)
            if split_actor:
                require(due <= uint(o["far"], 64, "split native FAR") < due + width,
                        "split native FAR must identify the actual eight-byte store")
            else:
                exact(o["far"], due, "peer second-push native FAR")
            exact(o["pc"], baseline.CODE_NEXT + fault_step * 64 + (8 if split_actor else 4), "native generated fault PC")
            exact(o["spsr"], fault_frame["nzcv"] | 0x3C4, "fault PSTATE")
        else:
            for field in ("esr", "far", "pc", "spsr"):
                exact(o[field], 0, "guarded actor must not invent native fault " + field)
        if not split_actor:
            for field in ("data_before_service", "data_after_service"):
                expected[field] = [0] * 8
            expected["dirty_before"] = expected["dirty_after"] = [0, 0]
            expected["fragments"] = [dict.fromkeys(("linear", "bytes", "gpa", "leaf", "before", "after", "ready"), 0) for _ in range(2)]
            route = walk(tables, initial["x"][8] - 8, write=True)
            exact(route["gpa"] & ~4095, expected["saved_page"], "actual first-push admitted route")
        else:
            service_before = [before[other_page + 4092 + i] for i in range(4)] + [before[old_page + i] for i in range(4)]
            expected["data_before_service"] = service_before
            expected["data_after_service"] = list(PUSH_VALUES[0].to_bytes(8, "little")) if profile == 3 else service_before
            expected["dirty_before"] = configured[1:]
            tables, routes, service_error = split_readiness(tables, BOUNDARY - 4)
            exact(service_error, error, "checked service guest-fault selection")
            expected["dirty_after"] = [table_word(tables, p) for p in (0x4820, 0x4828)]
            expected["fragments"] = []
            for i, (linear, width, route) in enumerate(routes):
                expected["fragments"].append({"linear": linear, "bytes": width, "gpa": route.get("gpa", 0),
                    "leaf": 0x4820 + i * 8, "before": configured[i + 1],
                    "after": route.get("after", configured[i + 1]), "ready": int("fault" not in route)})
        for field, value in expected.items():
            exact(o[field], value, f"owner {cpu} boundary {field}")
        for key, previous in (("final_lower_leaf", o["lower_before"]), ("final_upper_leaf", o["upper_before"])):
            leaves = list(previous)
            for index in (0, 256, 257, 260, 261):
                leaves[index] = 0
            leaves[256] = descriptor((other_page - DATA) // 4096)
            exact(o[key], leaves, f"owner {cpu} acknowledged full native leaf table")
        exact(o["bank_after"], record["prefix"]["cells"][cpu]["bank_after"], f"owner {cpu} cold guest bank preserved")
        exact(o["final_code"], [0] * 320, f"owner {cpu} code reclaimed after retained references")
        exact(o["current_el"], 4, "resident privilege")
        require(uint(o["sctlr"], 64, "SCTLR") & 0x1007 == 0x1005, "MMU/cache enabled and alignment trap disabled")
        require(uint(o["actlr"], 64, "ACTLR") & 2, "TSO enabled")
        exact(o["tcr"], baseline.TCR, "upper/lower native address regime")
        expected_owners.append(expected)
    exact(len(captures), 1, "same actual boundary return gateway on every owner")
    # Explicit final remap occurs after all native effects. It cannot retarget
    # any saved peer route, and it cannot mutate the actor's canonical fault.
    table_word(tables, 0x4800, other_page | 7)
    if profile == 1:
        table_word(tables, 0x4000, old_page | 7)
    if profile >= 3:
        table_word(tables, 0x4820, old_page | 7)
        table_word(tables, 0x4828, other_page | 7)
    for address in (DATA, HIGH + DATA):
        route = walk(tables, address)
        exact(route["gpa"], other_page, "acknowledgement routes through replacement mapping")
    exact(record["data_after"], [memory[DATA + i] for i in range(8192)], "complete physical boundary memory history")
    exact(record["tables_after"], tables, "complete guest tables after boundary and acknowledgements")
    exact(record["source_after"], list(baseline.NEW_SOURCE), "guest source survives code reclamation")
    exact(shared["code_at_outcome"], baseline.code_image(True), "actual code intact with peer references retained")
    return expected_owners


def check_schedule(shared, n, profile, actor):
    """Replay retained native operations through actor outcome, remap and reclaim."""
    wave = shared["wave"]
    trace = wave["trace"]
    require(type(trace) is list and 0 < len(trace) <= 768, "bounded boundary ownership trace")
    all_owners, peers = set(range(n)), set(range(n)) - {actor}
    delayed = (actor + n - 1) % n
    admitted, detached, completed, acknowledged, released, rejected = (set() for _ in range(6))
    queue, head, service = [], 0, None
    setup = actor_done = terminal = serviced = remapped = reclaimed = exclusive = False
    resumed, previous = set(), None
    for word in trace:
        uint(word, 64, "boundary trace event")
        event, cpu = word >> 8, word & 255
        require(cpu < n, "boundary trace owner outside configured set")
        if event == 40:
            require(not setup and not admitted, "unique setup precedes native admission")
            setup = True
        elif event == 1:
            require(setup and cpu not in admitted and not detached and not actor_done,
                    "unique initial admission before any native fault detaches")
            admitted.add(cpu)
        elif event == 2:
            require(admitted == all_owners and cpu in peers and cpu not in detached and not actor_done,
                    "retained peer fault requires all real native admissions")
            if n >= 3 and cpu == delayed:
                exact(detached, peers - {cpu}, "delayed peer must detach after all other peers")
            detached.add(cpu)
            queue.append(cpu)
        elif event == 42:
            exact(previous, (2, cpu), "saved native fault follows the actual detach")
        elif event in (44, 45):
            require(profile >= 3 and cpu == actor and not serviced and not actor_done,
                    "single checked split service for actor")
            exact(detached, peers, "split service waits for all retained peer faults")
            exact(event, 44 if profile == 3 else 45, "checked store readiness outcome")
            serviced = True
        elif event == 43:
            require(cpu == actor and profile != 3 and not terminal and not actor_done,
                    "only the faulting actor publishes a terminal guest packet")
            exact(detached, peers, "terminal packet published while every peer is detached")
            require(profile < 3 or serviced, "split guest fault must follow checked readiness")
            terminal = True
        elif event == 47:
            require(cpu == actor and not actor_done and not completed and admitted == all_owners,
                    "actor outcome cannot follow early peer completion")
            exact(detached, peers, "actor outcome retains every peer operation")
            exact(terminal, profile != 3, "precise terminal versus successful actor outcome")
            exact(serviced, profile >= 3, "split service completed before actor outcome")
            completed.add(cpu)
            actor_done = True
        elif event == 3:
            require(actor_done and peers and not exclusive and cpu == 0,
                    "exclusive FIFO service follows terminal actor isolation")
            exclusive = True
        elif event == 4:
            require(exclusive and service is None and head < len(queue), "one FIFO service owner")
            exact(cpu, queue[head], "retained peer FIFO selection")
            service = cpu
        elif event == 46:
            require(actor_done and service == cpu and cpu not in resumed and not remapped,
                    "saved route resumes once after actor outcome and before remap")
            resumed.add(cpu)
        elif event == 5:
            require(service == cpu and cpu in resumed and cpu not in completed,
                    "peer completion requires its selected real native continuation")
            completed.add(cpu)
            service, head = None, head + 1
        elif event == 48:
            require(completed == all_owners and service is None and not remapped,
                    "mapping mutation drains every admitted actor/peer effect")
            remapped = True
        elif event == 49:
            require(remapped and cpu not in acknowledged and not released,
                    "unique mapping acknowledgement before release")
            if cpu == delayed:
                exact(acknowledged, all_owners - {cpu}, "delayed mapping acknowledgement arrives last")
            acknowledged.add(cpu)
        elif event == 50:
            require(acknowledged == all_owners and cpu not in released and not reclaimed,
                    "retained code release requires every mapping acknowledgement")
            if cpu == delayed:
                exact(released, all_owners - {cpu}, "last retained owner delays reclamation")
            released.add(cpu)
        elif event == 51:
            require(released == all_owners and not reclaimed and previous == (50, cpu),
                    "actual code reclamation follows the last reference release")
            reclaimed = True
        elif event == 52:
            require(reclaimed and cpu not in rejected, "entry rejection follows actual reclamation")
            rejected.add(cpu)
        else:
            raise ValueError(f"unknown boundary ownership event {event}")
        if event != 42 and previous and previous[0] == 2:
            raise ValueError("native detach lacks its immediate saved-fault record")
        previous = event, cpu
    require(setup and actor_done and remapped and reclaimed and rejected == all_owners,
            "incomplete terminal-wave ownership closure")
    exact(resumed, peers, "every real peer operation resumes exactly once")
    all_bits, peer_bits = (1 << n) - 1, sum(1 << p for p in peers)
    for name, value in (("ready", all_bits), ("setup", 1), ("actor_done", 1), ("done", all_bits),
                        ("remapped", 1), ("acknowledgements", all_bits), ("released", all_bits),
                        ("reclaimed", 1), ("reuse_blocked", all_bits), ("entry_rejected", all_bits),
                        ("retained_before_release", all_bits)):
        exact(shared[name], value, "boundary ownership " + name)
    expected = {"participants": n, "scenario": 0, "alias_profile": 0, "stack_profile": 0,
                "mixed_profile": 0, "mixed_compiled": 0, "ready": all_bits, "finished": all_bits,
                "phase": 2, "active": 0, "queued": peer_bits,
                "queue": queue + [MASK64] * (4 - len(queue)), "head": len(peers), "length": len(peers),
                "servicing": MASK64, "completed": all_bits, "retained": 0, "generation": 2,
                "acknowledgements": 0, "new_entries": 0, "new_completed": 0, "new_references": 0,
                "reclaimed": 1, "old_eligible": 0, "published": 1, "stale_replies": 0,
                "stale_entries": 0, "reuse_blocked": 0, "translation_wait": 0,
                "event_pending": [0] * 4, "event_notifications": [0] * 4, "trace_length": len(trace)}
    exact({key: value for key, value in wave.items() if key not in ("lock", "next_ticket", "trace")},
          expected, "complete boundary coordinator")
    require(uint(wave["lock"], 64, "released coordinator lock") > 0, "boundary coordinator never acquired")
    exact(wave["lock"], uint(wave["next_ticket"], 64, "coordinator tickets"), "no owner stranded holding/waiting on lock")
    return len(trace)


def audit_development(record, **expected):
    envelope = check_envelope(record, **expected)
    n, profile, actor, _, shared, _ = envelope
    effects = check_effects(record, envelope)
    events = check_schedule(shared, n, profile, actor)
    return {"owners": n, "profile": profile, "actor": actor, "trace_events": events,
            "completed_instructions": sum(bin(e["completed"]).count("1") for e in effects),
            "native_faults": sum(e["faults"] for e in effects),
            "checked_services": sum(e["service_attempts"] for e in effects)}


def fields(record, names, label):
    require(type(record) is dict and record.keys() == set(names.split()), label + ": unexpected or missing fields")


def check(record, expected_profile=None, expected_vcpus=None, expected_actor=None, expected_region=None):
    """Accept only the current complete boundary profile and explicit identity."""
    fields(record, "case profile outcome region actor n configured_vcpus tested_vcpus prefix boundary owners data_after tables_after source_after", "boundary wrapper")
    fields(record["boundary"], "n profile actor ready setup actor_done done remapped acknowledgements released reclaimed reuse_blocked entry_rejected retained_before_release trace_start finished_before old_page other_page configured_leaves code_before code_at_outcome data_before tables_before wave", "resident boundary evidence")
    require(type(record["owners"]) is list, "boundary owners must be an array")
    for owner in record["owners"]:
        fields(owner, "id before_frame before_guest lower_before upper_before initial initial_guest current prefixes entries completed step fault_frame esr far pc spsr faults guest_after vector error address rip saved_page saved_writable native_pair_before native_pair_after service_attempts service_completed service_pc service_resume_pc fragments data_before_service data_after_service dirty_before dirty_after ready_mask retained_at_outcome ack_pair released final_frame final_guest entered status detail capture_pc current_el sctlr actlr tcr final_lower_leaf final_upper_leaf final_code bank_after", "owner boundary evidence")
    try:
        return audit_development(record, expected_profile=expected_profile, expected_vcpus=expected_vcpus,
                                 expected_actor=expected_actor, expected_region=expected_region)
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("malformed boundary evidence: " + str(error)) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--profile", type=int, choices=range(1, 6))
    parser.add_argument("--vcpus", type=int, choices=range(1, 5))
    parser.add_argument("--actor", type=int, choices=range(4))
    parser.add_argument("--region", choices=("low", "high"))
    args = parser.parse_args()
    for path in args.reports:
        result = check(json.loads(path.read_text()), args.profile, args.vcpus, args.actor, args.region)
        print(f"{path}: PASS (bounded stack boundary, {result['owners']} owners)")


if __name__ == "__main__":
    main()

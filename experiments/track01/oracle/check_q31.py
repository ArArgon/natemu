#!/usr/bin/env python3
"""Independent Q31 oracle. Synthetic preparation never executes a VM."""
import argparse
import copy
import json
from pathlib import Path
import struct

import check_q30 as q30

STATE = 0xE00000
MAGIC = 0x5133314641554C54
HEADER = "magic version ncpus failure cases phase checkpoint_records checkpoint_epoch".split()
CPU = "started done cases attempts deliveries commits code_writes snapshots timer_start timer_end depth current_attempt".split()
ARCH = "rax rcx rdx rbx rsp rbp rsi rdi r8 r9 r10 r11 r12 r13 r14 r15 rip rflags cs ss cr2".split()
META = "identity generation rip length instruction access width substep".split()
CPU_WORDS = len(CPU) + len(ARCH) + len(META)
STATE_BYTES = 64 + 4 * CPU_WORDS * 8 + 2 * 8192 + 1024
CASE, ATTEMPT, STATE_EVENT, STEP, DELIVER, DELIVERY_FAULT, RETURN, CODE, MARK, END = range(1, 11)
VARIANTS = {group: count for group, count in enumerate((1, 2, 4, 3, 2, 3, 3, 1, 11, 1, 1, 2), 1)}


class CodeState:
    """Replay code ownership and metadata pins alongside the paging protocol."""
    def __init__(self):
        self.roles = bytearray(1024)
        self.generations = [0] * 1024
        self.users = [0] * 1024
        self.pins = {}
        self.pending_generation = {}
        self.writes = [0] * 4
        self.pin_intervals = []
        self.generation_events = []

    def bind(self, memory, contexts):
        self.memory, self.contexts = memory, contexts

    def event(self, sequence, kind, cpu, epoch, data, phase, owner, active):
        if kind == q30.GATE and data[0] == 2:
            q30.require(not self.pending_generation, f"record {sequence}: code mutation published without generation")
        if kind != 13:
            return
        q30.require(data[0] in range(1, 11), f"record {sequence}: unknown Q31 metadata event")
        if data[0] == ATTEMPT:
            attempt, identity, rip, instruction, length, width, generation = data[1:]
            encoding = {1: b"\x48\x8b\x07", 2: b"\x48\x89\x07", 3: b"\x48\x0f\xc1\x07",
                        4: b"\xf3\xa4", 6: b"\x48\x89\x07"}.get(instruction)
            if encoding is not None:
                actual = bytearray()
                context = dict(zip(q30.CONTEXT, self.contexts[cpu]))
                context.pop("pge")
                for address in range(rip, rip + len(encoding)):
                    result = q30.walk(self.memory, gva=address, access="execute", **context)
                    q30.require(result["status"] == "ok", "instruction recovery metadata has no executable guest opcode")
                    actual.append(self.memory[result["gpa"]])
                q30.require(actual == encoding and length == len(encoding), "instruction recipe contradicts coherent guest opcode bytes")
        if data[0] != CODE:
            return
        action, page, old, new, operation, generation, users = data[1:]
        q30.require(0 <= page < q30.RAM_SIZE and page & 4095 == 0 and action in (1, 2, 3, 4),
                    f"record {sequence}: invalid code ownership event")
        index = page // 4096
        if action in (1, 2):
            q30.require(phase == 1 and owner == cpu and not active,
                        f"record {sequence}: code ownership/generation outside exclusive quiescence")
        else:
            q30.require(phase == 0 and not self.pending_generation,
                        f"record {sequence}: code metadata pin raced exclusive publication")
        if action == 1:
            q30.require(old == self.roles[index] and new in (0, 1) and old != new and operation == 0,
                        f"record {sequence}: code role old/new disagreement")
            q30.require(new or self.users[index] == 0, f"record {sequence}: retired code metadata still pinned")
            self.roles[index] = new
            if new:
                self.pending_generation[page] = 0
        elif action == 2:
            q30.require(self.roles[index] and self.pending_generation.get(page) == operation
                        and old == self.generations[index] and new == old + 1,
                        f"record {sequence}: missing/duplicate/wrong code generation")
            self.generations[index] = new
            del self.pending_generation[page]
            self.generation_events.append((sequence, page, new, operation))
        elif action == 3:
            q30.require(self.roles[index] and old == self.users[index] and new == old + 1 and operation == 0
                        and not self.pins.get((cpu, page)),
                        f"record {sequence}: stale code pin or users disagreement")
            self.users[index] = new
            self.pins.setdefault((cpu, page), []).append(generation)
            self.pin_intervals.append(dict(cpu=cpu, page=page, generation=generation, start=sequence, stop=None))
        else:
            pins = self.pins.get((cpu, page), [])
            q30.require(pins and old == self.users[index] and new == old - 1 and operation == 0,
                        f"record {sequence}: foreign/missing code metadata pin")
            pins.pop()
            self.users[index] = new
            interval = next(item for item in reversed(self.pin_intervals)
                            if item["cpu"] == cpu and item["page"] == page and item["stop"] is None)
            interval["stop"] = sequence
        q30.require(generation == self.generations[index] and users == self.users[index],
                    f"record {sequence}: code generation/users annotation disagreement")
        return action in (1, 2)

    def mutation(self, sequence, cpu, address, cause, operation, phase, owner, active):
        page = address & ~4095
        if not self.roles[page // 4096]:
            q30.require(cause != q30.CODE, f"record {sequence}: CODE mutation without ownership")
            return
        q30.require(phase == 1 and owner == cpu and not active and page not in self.pending_generation,
                    f"record {sequence}: code bytes changed without exclusive generation protocol")
        self.pending_generation[page] = operation
        if cause in (q30.GUEST, q30.CODE):
            self.writes[cpu] += 1


def decode(ram, ncpus, version=3):
    q30.require(version in (3, 4, 5, 6) and len(ram) == ({3: q30.RAM_BYTES_V3, 4: q30.RAM_BYTES_V4, 5: q30.RAM_BYTES_V5, 6: q30.RAM_BYTES_V6}[version]),
                "Q31 complete RAM size/version")
    header = dict(zip(HEADER, struct.unpack_from("<8Q", ram, STATE)))
    q30.require((header["magic"], header["version"], header["ncpus"], header["failure"])
                == (MAGIC, 1, ncpus, 0), "Q31 identity/version/failure")
    cpus = []
    for cpu in range(4):
        values = struct.unpack_from(f"<{CPU_WORDS}Q", ram, STATE + 64 + cpu * CPU_WORDS * 8)
        if cpu >= ncpus:
            q30.require(not any(values), "unused Q31 CPU state")
            continue
        item = dict(zip(CPU, values[:12]))
        item["arch"] = dict(zip(ARCH, values[12:33]))
        item["metadata"] = dict(zip(META, values[33:41]))
        cpus.append(item)
    offset = STATE + 64 + 4 * CPU_WORDS * 8
    generations = struct.unpack_from("<1024Q", ram, offset)
    users = struct.unpack_from("<1024Q", ram, offset + 8192)
    roles = bytes(ram[offset + 16384:offset + 17408])
    q30.require(all(role in (0, 1) for role in roles), "invalid code ownership bit")
    return header, cpus, generations, users, roles


def snapshots(records, after=0):
    """Recover full per-CPU architectural snapshots, retaining event bounds."""
    pending, completed, counts = {}, [], {}
    for sequence, kind, cpu, epoch, data in records:
        if kind != 13:
            continue
        q30.require(data[0] in range(1, 11), f"record {sequence}: unknown Q31 event")
        if data[0] != STATE_EVENT:
            q30.require(cpu not in pending, f"record {sequence}: interrupted Q31 state snapshot")
            continue
        identity, point, attempt, chunk, *values = data[1:]
        if chunk == 0:
            q30.require(cpu not in pending and identity == counts.get(cpu, 0) + 1,
                        f"record {sequence}: duplicate/missing Q31 snapshot")
            pending[cpu] = dict(identity=identity, point=point, attempt=attempt, cpu=cpu,
                                sequence=sequence, chunks=[], epoch=epoch)
        snapshot = pending.get(cpu)
        q30.require(snapshot is not None and (identity, point, attempt) ==
                    (snapshot["identity"], snapshot["point"], snapshot["attempt"])
                    and chunk == len(snapshot["chunks"]) and chunk < 7,
                    f"record {sequence}: mixed/out-of-order Q31 snapshot")
        snapshot["chunks"].append(values)
        if chunk == 6:
            snapshot["arch"] = dict(zip(ARCH, [value for row in snapshot.pop("chunks") for value in row]))
            snapshot["end_sequence"] = sequence
            if sequence > after:
                completed.append(snapshot)
            counts[cpu] = identity
            del pending[cpu]
    q30.require(not pending, "unfinished Q31 state snapshot")
    return completed


def index_trace(records, operations, ncpus, prefix_records, variant_counts=None, initial_counts=None, interrupt_mode=False, live_pin_recipes=False, additional_instructions=()):
    """Bind every post-checkpoint native operation to its retained state recipe."""
    state_at = {state["end_sequence"]: state for state in snapshots(records, prefix_records)}
    op_by_id = {op["sequence"]: op for op in operations if op["sequence"] > prefix_records}
    cases, active_case, recipes, metadata, used = {}, None, {}, {}, set()
    variants = VARIANTS if variant_counts is None else variant_counts
    counts = [dict(attempts=0, deliveries=0, commits=0, cases=0, snapshots=0) for _ in range(ncpus)]
    if initial_counts is not None:
        for cpu in range(ncpus):
            counts[cpu].update({key: initial_counts[cpu][key] for key in ("attempts", "deliveries", "commits", "snapshots")})
    substeps = [0] * ncpus
    recipe_pins = {}
    for sequence, kind, cpu, epoch, data in records:
        if live_pin_recipes and kind == 13 and data[0] == CODE:
            if data[1] == 3:
                q30.require(cpu not in recipe_pins, "nested live code recipe pin")
                recipe_pins[cpu] = data[6]
            elif data[1] == 4:
                q30.require(cpu in recipe_pins, "live code recipe unpin without owner")
                del recipe_pins[cpu]
        if sequence <= prefix_records or kind != 13:
            continue
        subtype, payload = data[0], data[1:]
        if subtype == CASE:
            group, variant, rep, start, serial, stated_cpus, reserved = payload
            key = group, variant, rep
            q30.require(cpu == 0 and group in variants and variant < variants[group] and rep < 16
                        and start in (0, 1) and stated_cpus == ncpus and reserved == 0,
                        f"record {sequence}: invalid Q31 finite case")
            if start:
                q30.require(active_case is None and key not in cases and serial == len(cases) + 1 and not recipes,
                            f"record {sequence}: duplicate/nested Q31 case")
                active_case = key
                cases[key] = dict(start=sequence, stop=None, serial=serial,
                                  events=[[] for _ in range(ncpus)], states=[[] for _ in range(ncpus)],
                                  attempts=[[] for _ in range(ncpus)], steps=[[] for _ in range(ncpus)], ends={})
                metadata.clear()
                substeps = [0] * ncpus
            else:
                q30.require(active_case == key and cases[key]["serial"] == serial and not recipes
                            and set(cases[key]["ends"]) == set(range(ncpus)),
                            f"record {sequence}: incomplete Q31 case boundary")
                cases[key]["stop"] = sequence
                active_case = None
            continue
        q30.require(active_case is not None, f"record {sequence}: Q31 event outside finite case")
        case = cases[active_case]
        q30.require(cpu not in case["ends"], f"record {sequence}: CPU emitted metadata after case completion")
        if subtype != STATE_EVENT or sequence in state_at:
            case["events"][cpu].append((sequence, subtype, payload))
        if subtype == ATTEMPT:
            identity, meta_id, rip, instruction, length, width, generation = payload
            q30.require(cpu not in recipes and identity == counts[cpu]["attempts"] + 1 and meta_id == rip
                        and (instruction in range(1, 17 if interrupt_mode else 8) or instruction in additional_instructions) and length in range(1, 16) and width in (1, 8)
                        and generation > 0, f"record {sequence}: invalid attempt metadata")
            counts[cpu]["attempts"] += 1
            metadata[cpu] = dict(sequence=sequence, identity=identity, rip=rip, instruction=instruction,
                                 length=length, width=width, generation=generation)
            case["attempts"][cpu].append(metadata[cpu])
            substeps[cpu] = 0
        elif subtype == MARK and payload[0] == 1:
            _, attempt, rip, instruction, substep, access_width, generation = payload
            meta = metadata.get(cpu)
            q30.require(cpu not in recipes and attempt == (meta["identity"] if meta else 0)
                        and substep == substeps[cpu] + 1
                        and instruction == (meta["instruction"] if meta else 1)
                        and generation == (meta["generation"] if meta and rip >> 12 == meta["rip"] >> 12
                                           and (not live_pin_recipes or recipe_pins.get(cpu) == meta["generation"]) else 1),
                        f"record {sequence}: missing/stale Q31 access recipe")
            recipes[cpu] = dict(sequence=sequence, attempt=attempt, rip=rip, instruction=instruction,
                                substep=substep, access=access_width & 255, width=access_width >> 8, generation=generation)
            substeps[cpu] = substep
        elif subtype == STEP:
            attempt, action, status, error, operation_id, effects, value = payload
            recipe, operation = recipes.pop(cpu, None), op_by_id.get(operation_id)
            q30.require(recipe is not None and operation is not None and operation_id not in used
                        and operation["cpu"] == cpu and recipe["sequence"] < operation_id
                        and operation["end_sequence"] < sequence
                        and (attempt, action >> 8) == (recipe["attempt"], recipe["substep"])
                        and operation["begin"][1:3] == (recipe["access"], recipe["width"])
                        and (status, error, value) == (operation["end"][2], operation["end"][3], operation["end"][1])
                        and effects == counts[cpu]["commits"], f"record {sequence}: native operation/recipe/STEP mismatch")
            action &= 255
            allowed = {1: {(0, 8)}, 2: {(1, 8)}, 3: {(0, 8)}, 4: {(1, 8)}, 5: {(0, 8)},
                       6: {(0, 8), (1, 8), (1, 1)}, 7: {(4, 1), (3, 8)}, 8: {(0, 1)}, 9: {(1, 1)}}
            if interrupt_mode:
                allowed[6].add((0, 1))
            q30.require(action in allowed and operation["begin"][1:3] in allowed[action],
                        f"record {sequence}: substep operand kind/width mismatch")
            q30.require(action not in (3, 4, 5, 6) or operation["context"][1] == 0,
                        f"record {sequence}: descriptor/frame access lost implicit supervisor privilege")
            used.add(operation_id)
            case["steps"][cpu].append(dict(sequence=sequence, action=action, recipe=recipe, operation=operation))
        elif subtype == STATE_EVENT and sequence in state_at:
            state = state_at[sequence]
            q30.require(state["point"] & 255 in range(1, 7) and state["point"] >> 8 <= 2
                        and state["attempt"] == metadata.get(cpu, {"identity": 0 if interrupt_mode else None})["identity"],
                        f"record {sequence}: architectural snapshot attempt/depth")
            counts[cpu]["snapshots"] += 1
            counts[cpu]["commits"] += (state["point"] & 255) == 5
            case["states"][cpu].append(state)
        elif subtype == DELIVER:
            q30.require(payload[0] == metadata.get(cpu, {"identity": 0 if interrupt_mode else None})["identity"], "delivery uses foreign instruction attempt")
            counts[cpu]["deliveries"] += 1
        elif subtype in (DELIVERY_FAULT, RETURN):
            q30.require(payload[0] == metadata.get(cpu, {"identity": 0 if interrupt_mode else None})["identity"], "exception recovery uses foreign instruction attempt")
        elif subtype == MARK:
            permitted = {3: active_case[0] == 9 and active_case[1] in (6, 7, 8),
                         4: active_case[0] == 12, 5: active_case[0] in (10, 11),
                         6: active_case[:2] == (12, 1)}
            q30.require(payload[0] == 5 if interrupt_mode else permitted.get(payload[0], False), "unknown or misplaced Q31 control marker")
            if payload[0] == 3:
                q30.require(payload[1] == metadata[cpu]["identity"], "DF termination uses foreign instruction attempt")
        elif subtype == END:
            serial, group, variant, rep, commits, deliveries, timer = payload
            q30.require((group, variant, rep) == active_case and serial == case["serial"]
                        and cpu not in case["ends"] and (interrupt_mode or commits == counts[cpu]["commits"]
                        and deliveries == counts[cpu]["deliveries"]), f"record {sequence}: Q31 case effect counters")
            counts[cpu]["cases"] += 1
            case["ends"][cpu] = dict(sequence=sequence, timer=timer)
    required = {(group, variant, rep) for group, count in variants.items() for variant in range(count) for rep in range(16)}
    q30.require(active_case is None and set(cases) == required and not recipes and used == set(op_by_id),
                "missing Q31 cases or native operations without retained recipes")
    return cases, counts


def add_flags(left, right, old_flags):
    total = left + right
    value = total & q30.MASK
    flags = int(total > q30.MASK) | (4 if bin(value & 255).count("1") % 2 == 0 else 0)
    flags |= (left ^ right ^ value) & 16
    flags |= (64 if value == 0 else 0) | (128 if value >> 63 else 0)
    flags |= 2048 if ((left >> 63) == (right >> 63) and (value >> 63) != (left >> 63)) else 0
    return (old_flags & ~0x8D5) | flags


def attempt_semantics(case, cpu, group, interrupts=None, continuity=True, boundary_states=None, scalar_fetches=False):
    states, steps = case["states"][cpu], case["steps"][cpu]
    for previous, state in zip(states, states[1:]):
        if continuity and previous["point"] & 255 == 4 and state["point"] & 255 == 1:
            q30.require(state["arch"] == previous["arch"], "retry did not use exactly restored IRET state")
    for attempt in case["attempts"][cpu]:
        own = [s for s in states if s["attempt"] == attempt["identity"]]
        before = [s for s in own if s["point"] & 255 == 1]
        q30.require(len(before) == 1 and before[0]["arch"]["rip"] == attempt["rip"], "attempt lacks exact before-state")
        old = before[0]["arch"]
        expected = dict(old)
        final = [s for s in own if s["point"] & 255 == 5]
        faults = [s for s in own if s["point"] & 255 == 2]
        limit = min([s["sequence"] for s in final + faults] or [case["stop"]])
        native = [s for s in steps if s["recipe"]["attempt"] == attempt["identity"]
                  and s["recipe"]["rip"] == attempt["rip"] and s["action"] in (1, 2, 7, 8, 9)
                  and before[0]["end_sequence"] < s["sequence"] < limit]
        instruction = attempt["instruction"]
        q30.require((attempt["length"], attempt["width"]) in
                    ({1: {(3, 8)}, 2: {(3, 8)}, 3: {(4, 8)}, 4: {(2, 1)}, 5: {(3, 1), (5, 1)} | ({(1, 1)} if interrupts is not None else set()),
                      6: {(3, 8)}, 7: {(2, 1)}}[instruction]),
                    "instruction metadata length/width outside fixed x86 encodings")
        if scalar_fetches and instruction in (1, 2, 6):
            fetched = [step["operation"] for step in native if step["action"] == 7]
            if fetched:
                expected_opcode = b"\x48\x8b\x07" if instruction == 1 else b"\x48\x89\x07"
                q30.require(len(fetched) == 3 and all(op["begin"][:3] == (attempt["rip"] + i, 4, 1)
                            and op["end"][2] == 0 and op["end"][1] == expected_opcode[i] for i, op in enumerate(fetched)),
                            "scalar explicit instruction fetch differs from decoded opcode")
            native = [step for step in native if step["action"] != 7]
        operations = [step["operation"] for step in native]
        failed = next((p for p in operations if p["end"][2]), None)
        if instruction in (1, 2, 6):
            q30.require(len(operations) == 1, "scalar instruction missing/duplicate native operand")
            operation = operations[0]
            q30.require(operation["begin"][:3] == (old["rdi"], 0 if instruction == 1 else 1, 8),
                        "scalar instruction did not use retained RDI operand")
            if instruction != 1:
                q30.require(operation["begin"][3] == old["rax"], "MOV store lost retained RAX source")
            elif failed is None:
                expected["rax"] = operation["end"][1]
        elif instruction == 3:
            q30.require(len(operations) in (1, 2) and operations[0]["begin"][:3] == (old["rdi"], 0, 8),
                        "XADD missing native old-value load")
            if operations[0]["end"][2] == 0:
                value = operations[0]["end"][1]
                q30.require(len(operations) == 2 and operations[1]["begin"][:4] ==
                            (old["rdi"], 1, 8, (value + old["rax"]) & q30.MASK), "XADD sum/store operand mismatch")
                expected["rax"] = value
                expected["rflags"] = add_flags(value, old["rax"], old["rflags"])
        elif instruction == 4:
            progress = [s for s in own if s["point"] & 255 == 6 and s["sequence"] < limit
                        and s["end_sequence"] not in (boundary_states or set())]
            completed = 0
            direction = -1 if old["rflags"] & (1 << 10) else 1
            for offset in range(0, len(operations), 2):
                q30.require(offset + 1 < len(operations) and expected["rcx"] > 0, "REP operand/count mismatch")
                source, destination = operations[offset:offset + 2]
                q30.require(source["begin"][:3] == (expected["rsi"], 0, 1) and source["end"][2] == 0
                            and destination["begin"][:4] == (expected["rdi"], 1, 1, source["end"][1]),
                            "REP source/destination byte or progress mismatch")
                if destination["end"][2]:
                    q30.require(offset + 2 == len(operations), "REP continued after denied element")
                    break
                expected["rsi"] = (expected["rsi"] + direction) & q30.MASK
                expected["rdi"] = (expected["rdi"] + direction) & q30.MASK
                expected["rcx"] -= 1
                q30.require(completed < len(progress) and progress[completed]["arch"] == expected,
                            "REP exposed wrong completed-iteration state")
                completed += 1
            q30.require(completed == len(progress), "REP invented/duplicated progress snapshot")
        elif instruction == 7:
            q30.require(old["rax"] == old["rcx"] == 0 and len(operations) == 2
                        and [p["begin"][:3] for p in operations] == [(old["rip"], 4, 1), (old["rip"] + 1, 4, 1)]
                        and all(p["end"][2] == 0 for p in operations)
                        and bytes(p["end"][1] for p in operations) == b"\x0f\xa2", "CPUID did not fetch its actual guest encoding")
            expected.update(rax=0, rbx=0x6574614E, rcx=0x2165626F, rdx=0x7250756D)
            marks = [d for seq, subtype, d in case["events"][cpu]
                     if subtype == MARK and d[0] == 5 and attempt["sequence"] < seq < limit]
            q30.require(marks == [(5, 0, expected["rax"], expected["rbx"], expected["rcx"], expected["rdx"], 0)],
                        "CPUID leaf/vendor result mismatch")
        else:
            fetches = [p for p in operations if p["begin"][1] == 4]
            q30.require(fetches and [p["begin"][0] for p in fetches] ==
                        list(range(old["rip"], old["rip"] + len(fetches))), "instruction fetch skipped/repeated opcode byte")
            if failed is None:
                q30.require(len(fetches) == attempt["length"] and len(operations) == len(fetches) + 1,
                            "fetched instruction lacks its actual native effect")
                encoded = bytes(p["end"][1] for p in fetches)
                effect = operations[-1]
                if attempt["length"] == 3:
                    q30.require(encoded == b"\x48\x8b\x07" and effect["begin"][:3] == (old["rdi"], 0, 8),
                                "fetched MOV bytes did not select their operand")
                else:
                    q30.require(encoded[0] == 0xB8 and int.from_bytes(encoded[1:], "little") in (42, 43)
                                and effect["begin"][1:3] == (3, 8)
                                and effect["end"][1] == int.from_bytes(encoded[1:], "little"),
                                "guest code bytes did not select native replacement")
                expected["rax"] = effect["end"][1]
        interrupted = faults and interrupts is not None and faults[0]["end_sequence"] in interrupts and faults[0]["sequence"] == limit
        if interrupted and failed is None:
            q30.require(instruction == 4 and expected["rcx"] > 0 and faults[0]["arch"] == expected and not final,
                        "IRQ interrupted outside a precise nonterminal REP iteration")
        elif failed is not None:
            q30.require(faults and not final, "faulting instruction committed or lacked recovery state")
            if instruction != 4:
                expected = dict(old)
            address, _, width = failed["begin"][:3]
            if failed["end"][2] == 14:
                expected["cr2"] = ((address & ~4095) + 4096
                                   if (address & 4095) + width > 4096 else address)
            q30.require(faults[0]["arch"] == expected, "fault state lost GPR/flags/progress/CR2 precision")
        elif final:
            q30.require(len(final) == 1 and (instruction != 4 or expected["rcx"] == 0), "instruction committed twice/incomplete REP")
            expected["rip"] += attempt["length"]
            expected["rflags"] &= ~(1 << 16)
            q30.require(final[0]["arch"] == expected, "wrong instruction commit/restart architectural state")
        else:
            q30.require(group == 12, "successful instruction omitted architectural commit")


def delivery_semantics(case, cpu, interrupts=None, interrupt_frame_builder=None, context_stops=None, fixture_base=None):
    """Check IDT delivery/IRET from actual coherent descriptor and stack accesses."""
    base = 0x10000000 + cpu * 0x20000 if fixture_base is None else fixture_base
    state_at = {s["end_sequence"]: s for s in case["states"][cpu]}
    step_at = {s["sequence"]: s for s in case["steps"][cpu]}
    pending, entry, current, expected_handler, expected_return = None, None, None, None, None
    segment, iret_reads, frames = [], [], []
    last_step = None
    events = list(case["events"][cpu])
    events.extend((sequence, 100, data) for sequence, data in (context_stops or {}).items())
    for sequence, subtype, data in sorted(events):
        if sequence in step_at:
            step = step_at[sequence]
            operation, action = step["operation"], step["action"]
            last_step = step
            if action in (3, 4, 5):
                q30.require(step["recipe"]["rip"] == current["rip"], "implicit access used a foreign architectural recovery RIP")
            if action == 5:
                iret_reads.append(operation)
            if pending is not None and action in (3, 4):
                segment.append(step)
            if operation["end"][2] and action not in (3, 4):
                address, _, width = operation["begin"][:3]
                address = (address & ~4095) + 4096 if (address & 4095) + width > 4096 else address
                q30.require(pending is None, "new original fault during unfinished delivery")
                entry = None
                pending = dict(vector=operation["end"][2], error=operation["end"][3], address=address,
                               cr2=address if operation["end"][2] == 14 else current["cr2"])
        if sequence in state_at:
            state = state_at[sequence]
            point = state["point"] & 255
            if point == 2:
                if interrupts and sequence in interrupts:
                    q30.require(pending is None and current is not None, "external IRQ replaced an unfinished fault")
                    specification = interrupts[sequence]
                    vector, partial = specification if isinstance(specification, tuple) else (specification, False)
                    pending = dict(vector=vector, partial_rep=partial, error=0, address=0, cr2=current["cr2"])
                    entry = None
                q30.require(pending is not None and state["arch"]["cr2"] == pending["cr2"],
                            "delivery fault snapshot lacks exact per-CPU CR2")
                if pending["vector"] != 8:
                    q30.require(state["arch"] == dict(entry if entry is not None else current, cr2=pending["cr2"]),
                                "delivery failure changed architectural state before handler transfer")
                entry, segment = dict(state["arch"]), []
            elif point == 3:
                q30.require(expected_handler is not None, "handler state without completed delivery")
                for key, value in expected_handler.items():
                    q30.require(state["arch"][key] == value, "handler live register/flags/stack mismatch: " + key)
                q30.require(state["point"] >> 8 == len(frames), "handler depth contamination")
                expected_handler = None
            elif point == 4:
                q30.require(expected_return == state["arch"] and state["point"] >> 8 == len(frames),
                            "IRET did not restore exact frame and current CR2")
                expected_return = None
            current = state["arch"]
        if subtype == DELIVERY_FAULT:
            attempt, first, second, error, address, phase, depth = data
            q30.require(pending is not None and first == pending["vector"] and depth == len(frames)
                        and phase in (1, 2, 3, 4) and last_step is not None,
                        "delivery-failure phase/ordered first event mismatch")
            operation = last_step["operation"]
            if second == 14:
                q30.require(operation["end"][2:4] == (14, error) and operation["begin"][0] == address
                            and last_step["action"] == (4 if phase == 4 else 3),
                            "delivery #PF not backed by its supervisor memory access")
                pending["cr2"] = address
            else:
                q30.require(second == 11 and phase in (1, 2) and operation["end"][2] == 0
                            and not operation["end"][1] & (1 << 47), "invented contributory delivery failure")
                if phase == 1:
                    wanted = (first << 3) | 3
                    low = next(s["operation"] for s in segment if s["operation"]["begin"][0] == base + 0x1000 + first * 16)
                    q30.require(not low["end"][1] & (1 << 47), "present gate reported absent")
                else:
                    wanted = ((operation["begin"][0] - base - 0x10000) & ~3) | 1
                q30.require(error == wanted and address == pending["address"], "wrong #NP selector/EXT context")
            vector = second if interrupts is not None and first >= 32 else delivery_pair(first, second)
            q30.require(vector != "shutdown", "fixture attempted recovery after failed #DF delivery")
            pending.update(vector=vector, error=0 if vector == 8 else error, address=address)
        elif subtype == DELIVER:
            attempt, vector, error, cr2, handler, rsp, depth = data
            q30.require(pending is not None and (vector, error, cr2) ==
                        (pending["vector"], pending["error"], pending["cr2"])
                        and entry is not None and depth == len(frames) + 1, "wrong or duplicate delivered exception")
            descriptors = [s["operation"] for s in segment if s["action"] == 3]
            pushes = [s["operation"] for s in segment if s["action"] == 4]
            external = interrupts is not None and vector >= 32
            q30.require(len(descriptors) in (3, 4) and len(pushes) == (5 if external else 6)
                        and all(p["end"][2] == 0 for p in descriptors + pushes), "incomplete actual IDT/frame accesses")
            location = base + 0x1000 + vector * 16
            q30.require([p["begin"][0] for p in descriptors[:2]] == [location, location + 8], "wrong IDT vector address")
            low, high = [p["end"][1] for p in descriptors[:2]]
            selector, ist, gate_type = (low >> 16) & 65535, (low >> 32) & 7, (low >> 40) & 15
            wanted_handler = (low & 65535) | (((low >> 48) & 65535) << 16) | (high << 32)
            q30.require(low & (1 << 47) and handler == wanted_handler
                        and handler == base + (0x3400 if external else {8: 0x3300, 13: 0x3200, 14: 0x3100}[vector])
                        and descriptors[2]["begin"][0] == base + 0x10000 + (selector & ~7),
                        "gate/descriptor did not select declared handler")
            descriptor = descriptors[2]["end"][1]
            q30.require(descriptor == 0x00AF9B000000FFFF, "handler selector lacks exact present 64-bit code descriptor")
            switched = bool(ist or entry["cs"] & 3)
            selected = entry["rsp"]
            if switched:
                q30.require(len(descriptors) == 4 and descriptors[3]["begin"][0] == base + 0x2000 + (28 + ist * 8 if ist else 4),
                            "wrong privilege/IST TSS stack source")
                selected = descriptors[3]["end"][1]
            else:
                q30.require(len(descriptors) == 3, "same-CPL IST0 unexpectedly selected TSS stack")
            builder = interrupt_frame_builder if external else fault_frame
            q30.require(builder is not None, "external interrupt lacks independent frame model")
            frame = builder(dict(entry, error=error, **({"partial_rep": pending.get("partial_rep", False)} if external else {})), selected, selector & ~3, gate_type, switched)
            q30.require(rsp == frame["rsp"] and [p["begin"][0] for p in pushes] == list(range(rsp + (32 if external else 40), rsp - 1, -8)),
                        "wrong frame alignment/slot order/width")
            slots = tuple(p["begin"][3] for p in reversed(pushes))
            if vector == 8:
                q30.require(slots[0] == 0, "double fault error code is not zero")
                expected_handler = {key: value for key, value in frame.items() if key in ("rsp", "cs", "ss")}
                expected_handler["rip"] = handler
            else:
                q30.require(slots == frame["slots"], "architectural frame lost RIP/CS/flags/old RSP/SS/error")
                expected_handler = dict(entry, rip=handler, rsp=rsp, cs=frame["cs"], ss=frame["ss"], rflags=frame["rflags"])
            frames.append((vector, rsp, slots))
            pending, segment = None, []
        elif subtype == RETURN:
            attempt, vector, rsp, rip, old_rsp, depth, reserved = data
            q30.require(frames and vector != 8 and (vector, rsp) == frames[-1][:2] and reserved == 0
                        and depth == len(frames) - 1 and len(iret_reads) == 5,
                        "orphan/duplicate/#DF IRET or incorrect depth")
            first = rsp if interrupts is not None and vector >= 32 else rsp + 8
            q30.require([p["begin"][0] for p in iret_reads] == list(range(first, first + 40, 8))
                        and all(p["end"][2] == 0 for p in iret_reads), "IRET bypassed coherent frame reads")
            values = tuple(p["end"][1] for p in iret_reads)
            saved = frames[-1][2] if interrupts is not None and vector >= 32 else frames[-1][2][1:]
            q30.require(values == saved and (rip, old_rsp) == (values[0], values[3]),
                        "IRET frame was corrupted or restored wrong RIP/RSP")
            expected_return = dict(current, rip=values[0], cs=values[1], rflags=values[2], rsp=values[3], ss=values[4])
            q30.require(values[1] & 3 != 3 or values[4] & ~3, "IRET accepted NULL user SS")
            frames.pop()
            iret_reads = []
        elif subtype == MARK and data[0] == 3:
            q30.require(len(frames) == 1 and frames[0][0] == 8 and data[2] == 8 and data[3] == 1,
                        "invalid explicit termination of double-fault context")
            frames.clear()
        elif subtype == 100:
            q30.require(len(frames) == 1 and frames[0][0] == 14 and data[0:3] == (3, 0x41, 14)
                        and data[3] == current["cr2"] and data[4] == frames[0][1] and data[5:] == (1, 0),
                        "invalid explicit stop after interrupt-delivery descriptor #PF")
            frames.clear()
    q30.require(not frames and pending is None and expected_handler is None and expected_return is None,
                "incomplete exception delivery/return state machine")


def fault_frame(old, selected_rsp, selector, gate_type, switched):
    """Intel 64-bit IDT fault frame; FRED/CET disabled in this finite fixture."""
    q30.require(gate_type in (14, 15) and selector & 3 == 0,
                "unsupported fault gate/handler privilege")
    rsp = (selected_rsp & ~15) - 48
    q30.require(0 <= rsp <= q30.MASK - 47, "fault frame address overflow")
    saved_flags = old["rflags"] | (1 << 16)
    live_flags = old["rflags"] & ~((1 << 8) | (1 << 16) | (1 << 14) | (1 << 17))
    if gate_type == 14:
        live_flags &= ~(1 << 9)
    return dict(rsp=rsp, slots=(old["error"], old["rip"], old["cs"], saved_flags, old["rsp"], old["ss"]),
                cs=selector, ss=0 if switched else old["ss"], rflags=live_flags)


def delivery_pair(first, second):
    """Ordered subset used by Q31; a running handler starts a fresh delivery."""
    contributory = {11, 12, 13}
    q30.require(first in contributory | {8, 14} and second in contributory | {14},
                "exception pair outside declared Q31 fixtures")
    if first == 8:
        return "shutdown"
    if first == 14 or first in contributory and second in contributory:
        return 8
    return second


def finite_semantics(cases, ncpus):
    """Require the declared finite cases, then validate their architectural effects."""
    for (group, variant, rep), case in cases.items():
        for cpu in range(ncpus):
            attempt_semantics(case, cpu, group)
            delivery_semantics(case, cpu)
            states, attempts = case["states"][cpu], case["attempts"][cpu]
            commits = [s for s in states if s["point"] & 255 == 5]
            delivered = [d for _, k, d in case["events"][cpu] if k == DELIVER]
            failures = [s["operation"] for s in case["steps"][cpu]
                        if s["action"] in (1, 2, 7, 8, 9) and s["operation"]["end"][2]]
            outcomes = [(p["end"][2], p["end"][3]) for p in failures]
            wanted = None
            if group == 1:
                wanted = (1, 1, [])
            elif group in (2, 3, 8):
                error = ([7, 5, 3, 9][variant] if group == 3 else
                         6 if (cpu & 1 if group == 8 else variant) else 0)
                wanted = (2, 1, [(14, error)])
            elif group == 4:
                wanted = (1, 0, [(13, 0)]) if variant == 2 else (2, 1, [(14, 17 if variant == 0 else 16)])
            elif group == 5:
                wanted = (3, 2, [(14, 3 if variant else 0)])
            elif group == 6:
                split_semantics(case, cpu)
                if variant < 2:
                    wanted = (2, 1, [(14, 3 if variant else 2)])
                else:
                    q30.require(all(outcome == (14, 3) for outcome in outcomes)
                                and len(attempts) == 4 + len(outcomes) and len(commits) == 4,
                                "cross-page revocation omitted a round or exact restart")
            elif group == 7:
                wanted = (1, 1, []) if variant == 1 else (2, 1, [(14, 2)])
                progress = [s for s in states if s["point"] & 255 == 6]
                q30.require(len(progress) == (0 if variant == 1 else 8), "REP omitted completed elements")
                if variant != 1:
                    before = [s for s in states if s["point"] & 255 == 1]
                    q30.require([s["arch"]["rcx"] for s in before] == [8, 4], "REP restart lost remaining count")
            elif group == 9:
                if variant < 5:
                    wanted = (1, 0, [(14, 6 if variant == 1 else 0)])
                elif variant == 5:
                    wanted = (3, 1, [(14, 0), (14, 0)])
                elif variant in (6, 7, 8):
                    wanted = (1, 0, [(14, 6)])
                elif variant == 9:
                    wanted = (2, 0, [(13, 0), (13, 0)])
                else:
                    wanted = (4, 2, [(14, 6), (14, 0)])
            elif group in (10, 11):
                wanted = (5, 4, [(14, 0)]) if group == 11 and cpu else (4, 4, [])
                q30.require([a["instruction"] for a in attempts] ==
                            ([5, 1, 1, 7, 5] if group == 11 and cpu else [5, 6, 7, 5]),
                            "code case omitted current-block effect, CPUID or fresh dispatch")
            elif group == 12:
                wanted = (3, 2, []) if variant == 1 else (2, 1, [])
                marks = [d for _, k, d in case["events"][cpu] if k == MARK and d[0] == 4]
                q30.require(len(marks) == 1 and marks[0] ==
                            (4, attempts[0]["identity"], variant, attempts[0]["rip"] ^ (8 if variant == 0 else 0),
                             attempts[0]["generation"] + (variant == 1), 1, 0),
                            "negative metadata identity/generation control missing")
                q30.require([a["instruction"] for a in attempts] == ([1, 2, 1] if variant else [1, 1]),
                            "invalid metadata control omitted successful valid follow-up")
                if variant:
                    marks = [d for _, k, d in case["events"][cpu] if k == MARK and d[0] == 6]
                    q30.require(len(marks) == 1, "stale dispatch-generation control missing")
                    _, page, old, new, stale, fresh, users = marks[0]
                    q30.require(page == 0x28C000 + cpu * 0x20000 and old > 0 and new == old + 1
                                and (stale, fresh, users) == (1, 1, 0), "stale/fresh dispatch result or generation disagreement")
                    code = [d for _, k, d in case["events"][cpu] if k == CODE]
                    q30.require([d[0] for d in code] == [1, 2, 2, 3, 4, 1]
                                and code[2][1:4] == (page, old, new)
                                and code[3] == (3, page, 0, 1, 0, new, 1)
                                and code[4] == (4, page, 1, 0, 0, new, 0),
                                "negative stale dispatch was accepted or valid fresh pin lacked real lifetime events")
            if wanted:
                q30.require((len(attempts), len(commits), outcomes) == wanted,
                            f"case {group}/{variant}/{rep} CPU {cpu}: finite attempt/commit/fault signature")
            expected_delivery = [(8, 0)] if group == 9 and variant in (6, 7, 8) else outcomes
            if group == 9 and variant == 9:
                expected_delivery = [(14, 0), (13, 0)]
            q30.require([(d[1], d[2]) for d in delivered] == expected_delivery,
                        "finite case delivered wrong exception vector/error/order")
            base = 0x10000000 + cpu * 0x20000
            first = next(s["arch"] for s in states if s["point"] & 255 == 1)
            expected = dict(zip(ARCH[:16], range(0x700000 + cpu * 256, 0x700010 + cpu * 256)))
            expected.update(rsp=base + 0x5FF9, rip=base + 0x3000, rflags=0x202, cs=8, ss=16, cr2=0xDECAF000 + cpu)
            if group in (1, 2, 3, 8, 12):
                user = (variant == 1 if group == 2 else variant < 2 if group == 3 else cpu & 1 if group == 8 else False)
                store = (user if group in (2, 8) else variant in (0, 2) if group == 3 else False)
                expected["rdi"] = base + (0xD000 if group in (2, 8) else 0xC000)
                if user:
                    expected.update(cs=27, ss=35)
                if store:
                    expected["rax"] = 0xABCDEF0011223344
            elif group == 4:
                expected["rdi"] = 0x800000000000 if variant == 2 else base + 0xC080
                if variant < 2:
                    expected["rip"] = base + (0xC000 if variant == 0 else 0xCFFE)
            elif group == 5:
                expected.update(rip=base + 0x2FF0, rdi=base + 0xC080, rax=1)
            elif group == 6:
                expected.update(rdi=base + 0xCFFC, rax=0x1122334455667788)
            elif group == 7:
                expected.update(rcx=0 if variant == 1 else 8, rsi=base + 0xC000 + (7 if variant == 2 else 0),
                                rdi=base + (0xE003 if variant == 2 else 0xDFFC))
                expected["rflags"] |= (1 << 10) if variant == 2 else 0
            elif group == 9:
                user = variant == 1 or variant >= 6
                expected.update(rdi=0x800000000000 if variant == 9 else base + 0xD000,
                                rflags=(0x10302 if variant == 4 else 0x302) if variant < 5 else 0x202)
                if user:
                    expected.update(cs=27, ss=35)
                if user and variant != 9:
                    expected["rax"] = 0x12345678
            elif group in (10, 11):
                expected["rip"] = base + (0xC020 if group == 10 else 0x1B020)
            q30.require(first == expected, f"case {group}/{variant}/{rep} CPU {cpu}: wrong initial architectural operands/state")
    return len(cases)


def split_semantics(case, cpu):
    steps = case["steps"][cpu]
    base, backing = 0x10000000 + cpu * 0x20000, 0x280000 + cpu * 0x20000
    aliases = (base + 0x19FF8, base + 0x1E000)
    observed = [s for s in steps if s["operation"]["begin"][0] in aliases]
    q30.require(observed and len(observed) % 2 == 0, "split store lacks paired coherent half observations")
    for index in range(0, len(observed), 2):
        pair = observed[index:index + 2]
        for step, va, gpa in zip(pair, aliases, (backing + 0xCFF8, backing + 0xD000)):
            operation = step["operation"]
            physical = operation["end_walk"]["gpa"] if "end_walk" in operation else operation["end"][4]
            q30.require(step["action"] == 6 and operation["begin"][:3] == (va, 0, 8)
                        and operation["end"][2] == 0 and physical == gpa,
                        "split observation did not read both real physical destination halves")
    q30.require([s["operation"]["end"][1] for s in observed[:2]] ==
                [0x123456789ABCDEF0, 0x0FEDCBA987654321], "split initial sentinel halves disagreement")
    stores = [s for s in steps if s["action"] == 2]
    for store in stores:
        operation = store["operation"]
        before = [s for s in observed if s["sequence"] < store["sequence"]][-2:]
        after = [s for s in observed if s["sequence"] > store["sequence"]][:2]
        q30.require(len(before) == len(after) == 2, "split store omitted before/after observations")
        expected = [s["operation"]["end"][1] for s in before]
        if operation["end"][2] == 0:
            expected = [(expected[0] & 0xFFFFFFFF) | 0x5566778800000000,
                        (expected[1] & 0xFFFFFFFF00000000) | 0x11223344]
        else:
            fault = next(s for s in case["states"][cpu] if s["point"] & 255 == 2
                         and s["sequence"] > store["sequence"])
            q30.require(after[-1]["sequence"] < fault["sequence"], "denied split halves observed only after delivery/repair")
        q30.require([s["operation"]["end"][1] for s in after] == expected,
                    "split store exposed partial failure or wrong successful byte lanes")


def code_semantics(cases, code_state, ncpus, records):
    """Old pinned recovery remains valid; every fresh dispatch uses stable new bytes."""
    gates_by_epoch, invalidations = {}, [[] for _ in range(ncpus)]
    for record in records:
        if record[1] == q30.GATE:
            gates_by_epoch.setdefault(record[3], []).append(record)
        elif record[1] == q30.MARK and record[4][0] == 1 and record[4][2] == 0x33:
            invalidations[record[2]].append(record)
    for (group, variant, rep), case in cases.items():
        if group not in (10, 11):
            continue
        changes = []
        for cpu in range(ncpus):
            page = 0x28C000 if group == 11 else 0x28C000 + cpu * 0x20000
            own = [p for p in code_state.pin_intervals if p["cpu"] == cpu and p["page"] == page
                   and case["start"] < p["start"] < case["stop"]]
            q30.require(len(own) == 3 and all(p["stop"] is not None for p in own), "code case missing dispatch/current metadata pin")
            attempts = case["attempts"][cpu]
            for attempt in [a for a in attempts if a["instruction"] != 7]:
                pin = [p for p in own if p["start"] < attempt["sequence"] < p["stop"]]
                q30.require(len(pin) == 1 and attempt["generation"] == pin[0]["generation"],
                            "attempt used missing/stale/foreign pinned recovery metadata")
                states = [s for s in case["states"][cpu] if s["attempt"] == attempt["identity"]]
                q30.require(all(s["end_sequence"] < pin[0]["stop"] for s in states), "metadata retired before recovery/commit")
            for index, attempt in enumerate((attempts[0], attempts[-1])):
                operations = [s["operation"] for s in case["steps"][cpu] if s["recipe"]["attempt"] == attempt["identity"]]
                immediate = 42 + index
                q30.require(operations[-1]["end"][1] == immediate
                            and operations[-1]["end_walk"]["gpa"] == (0x4000 if immediate == 42 else 0x5000),
                            "fresh code bytes selected wrong immutable native implementation")
                q30.require(not any(p == page and own[index * 2]["start"] < seq < operations[-1]["end_sequence"]
                                    for seq, p, _, _ in code_state.generation_events),
                            "instruction dispatch spans changing code generation")
                for offset, operation in enumerate(operations[:5]):
                    q30.require(operation["end_walk"]["gpa"] == page + 0x20 + offset,
                                "code fetch escaped physical code ownership page")
            writes = [s["operation"] for s in case["steps"][cpu] if s["recipe"]["attempt"] == attempts[1]["identity"]
                      and s["action"] == 2]
            if group == 10 or cpu == 0:
                q30.require(len(writes) == 1 and writes[0]["begin"][3] == 0xC30000002BB8
                            and len(writes[0]["mutations"]) == 1, "code modification bypassed tracked native store")
                operation = writes[0]
                generation = [e for e in code_state.generation_events if e[1] == page and e[3] == operation["sequence"]]
                q30.require(len(generation) == 1 and own[1]["start"] < generation[0][0] < own[1]["stop"]
                            and own[2]["generation"] == own[1]["generation"] + 1,
                            "code write did not invalidate its current metadata generation")
                changes.append(generation[0])
            cpuid = next(a for a in attempts if a["instruction"] == 7)
            q30.require(own[1]["stop"] < cpuid["sequence"] < own[2]["start"], "new dispatch crossed no guest serialization boundary")
            commit = next(s for s in case["states"][cpu] if s["attempt"] == cpuid["identity"] and s["point"] & 255 == 5)
            target = 0x10000000 + cpu * 0x20000 + (0xC020 if group == 10 else 0x1B020)
            invalidate = [r for r in invalidations[cpu] if cpuid["sequence"] < r[0] < commit["sequence"]
                          and r[4][:3] == (1, target, 0x33)]
            q30.require(len(invalidate) == 1, "guest CPUID omitted actual global invalidation")
            epoch = invalidate[0][3]
            gates = gates_by_epoch.get(epoch, [])
            q30.require(len(gates) == 3 and [r[4][0] for r in gates] == [1, 2, 0]
                        and cpuid["sequence"] < gates[0][0] < invalidate[0][0] < gates[1][0] < gates[2][0] < commit["sequence"],
                        "guest CPUID retired before its gate/ACK epoch completed")
        if group == 11:
            q30.require(len(changes) == 1, "remote code case has wrong writer count")
            changed, page, generation, operation = changes[0]
            for cpu in range(1, ncpus):
                attempt = case["attempts"][cpu][1]
                pin = next(p for p in code_state.pin_intervals if p["cpu"] == cpu and p["page"] == page
                           and p["start"] < attempt["sequence"] < p["stop"])
                q30.require(pin["start"] < changed < pin["stop"] and pin["generation"] + 1 == generation,
                            "remote invalidation did not retain in-use old metadata")
                fault = next(s for s in case["states"][cpu] if s["attempt"] == attempt["identity"] and s["point"] & 255 == 2)
                q30.require(changed < fault["sequence"] < pin["stop"], "remote fault did not recover after old-generation invalidation")


def concurrent_semantics(cases, ncpus):
    observed = set()
    for (group, variant, rep), case in cases.items():
        if group not in (6, 8) or group == 6 and variant != 2:
            continue
        selected = []
        for cpu in range(ncpus):
            for step in case["steps"][cpu]:
                operation = step["operation"]
                if group == 8 and step["action"] in (1, 2) and operation["end"][2] == 14:
                    selected.append(operation)
                elif group == 6 and (step["action"] == 2 or step["action"] == 6
                                     and operation["begin"][0] & 0x1F000 == 0x1A000):
                    selected.append(operation)
        for left in selected:
            for right in selected:
                if left["cpu"] == right["cpu"]:
                    continue
                if group == 6 and (left["begin"][0] & 4095 != 0xFFC or right["begin"][0] & 0x1F000 != 0x1A000):
                    continue
                if group == 6 and ((right["cpu"] + 1) % ncpus != left["cpu"]
                                  or right["begin"][3] != 0x28D005 + left["cpu"] * 0x20000):
                    continue
                if (max(left["sequence"], right["sequence"]) < min(left["end_sequence"], right["end_sequence"])
                        and (group == 6 or left["faults"]) and right["faults"]):
                    observed.add(group)
    q30.require(observed == {6, 8}, "missing actual SMP revocation/fault-recovery overlap")


def checkpoint(checkpoint_ram, final_ram, initial, sym, version=3):
    """Check actual nonterminal Q30 bytes and their identity in the final log."""
    report = q30.check(checkpoint_ram, initial, sym=sym, version=version, terminal=False)
    q30.require(report["status"] == "PASS", "Q30 checkpoint lacks complete independent validation")
    prefix, prefix_cpus, _, _ = q30.decode(checkpoint_ram, initial, version=version, records_required=version < 4)
    final, final_cpus, _, _ = q30.decode(final_ram, initial, version=version, records_required=version < 4)
    q30.require(prefix["ncpus"] == final["ncpus"] and final["records"] > prefix["records"],
                "Q31 continuation missing or CPU count changed")
    end = q30.LOG_OFFSET + prefix["records"] * 96
    q30.require(memoryview(checkpoint_ram)[q30.LOG_OFFSET:end] == memoryview(final_ram)[q30.LOG_OFFSET:end],
                "Q30 log prefix changed/reset during Q31")
    q30.require(all(a["foundation"] == b["foundation"] for a, b in zip(prefix_cpus, final_cpus)),
                "saved foundation evidence changed during Q31")
    return report, prefix["records"]


def check(artifact, logs, sym, ram, initial, version=3, terminal=True, snapshot_only=False):
    q30.require(version in (3, 4, 5, 6) and (not snapshot_only or version >= 4 and not terminal),
                "explicit Q31 snapshot mode")
    checkpoint_ram = q30.snapshot(artifact("q30-checkpoint.bin"), version)
    prefix_report, prefix_records = checkpoint(checkpoint_ram, ram, initial, sym, version)
    header, paging_cpus, table_roles, records = q30.decode(ram, initial, version=version)
    ncpus = header["ncpus"]
    checkpoint_header, checkpoint_cpus, _, _ = q30.decode(checkpoint_ram, initial, version=version, records_required=version < 4)
    qheader, cpus, generations, users, roles = decode(ram, ncpus, version)
    q30.require(qheader["cases"] == 544 and qheader["phase"] == 2
                and (qheader["checkpoint_records"], qheader["checkpoint_epoch"]) ==
                (prefix_records, checkpoint_header["epoch"]), "Q31 final/checkpoint continuation identity")
    saved_header, saved_cpus, saved_generations, saved_users, saved_roles = decode(checkpoint_ram, ncpus, version)
    q30.require(saved_header == dict(qheader, cases=0, phase=0)
                and not any(saved_generations) and not any(saved_users) and not any(saved_roles)
                and not any(struct.unpack_from(f"<{4 * CPU_WORDS}Q", checkpoint_ram, STATE + 64)),
                "Q31 started or code metadata existed before actual Q30 checkpoint")
    q30.require(snapshot_only or artifact("q31.bin").read_bytes() == ram[STATE:STATE + STATE_BYTES], "separate Q31 dump disagrees")
    q30.require([r["q30_checkpoint_dump_complete"] for r in logs if "q30_checkpoint_dump_complete" in r] == [True]
                and [r["q30_checkpoint_host_checks_pass"] for r in logs if "q30_checkpoint_host_checks_pass" in r] == [True],
                "owning threads did not all stop at Q30 checkpoint")
    summary = next(r for r in logs if "checkpoint_ram_bytes" in r)
    q30.require((summary["checkpoint_ram_bytes"], summary["checkpoint_records"], summary["checkpoint_epoch"])
                == (len(ram), prefix_records, checkpoint_header["epoch"]), "checkpoint host/raw disagreement")
    for cpu in range(ncpus):
        stops = [r for r in logs if r.get("cpu") == cpu and "checkpoint_instruction_pc" in r]
        q30.require(len(stops) == 1, "missing owning-thread checkpoint registers")
        stop = stops[0]
        q30.require(stop["checkpoint_register_check"] is True and stop["checkpoint_actlr"] & 2
                    and stop["checkpoint_cpsr"] & 128 == 0
                    and stop["checkpoint_resume_pc"] == stop["checkpoint_instruction_pc"] + 4
                    and struct.unpack_from("<I", checkpoint_ram, stop["checkpoint_instruction_pc"] - 0x40000000)[0] == 0xD4000662
                    and (stop["checkpoint_tcr"], stop["checkpoint_ttbr0"], stop["checkpoint_ttbr1"]) ==
                    tuple(checkpoint_cpus[cpu]["final_" + name] for name in ("tcr", "ttbr0", "ttbr1")),
                    "checkpoint PC/TSO/IRQ/translation state disagreement")
    del checkpoint_ram
    code_state = CodeState()
    result = q30.replay(initial, records, ncpus, version=version, code_state=code_state)
    q30.foundation(ram, paging_cpus, sym, terminal=terminal, version=version)
    q30.validate_native_faults(records, sym)
    q30.validate_snapshot(ram, header, paging_cpus, table_roles, result)
    cases, counts = index_trace(records, result["operations"], ncpus, prefix_records)
    finite_semantics(cases, ncpus)
    code_semantics(cases, code_state, ncpus, records)
    concurrent_semantics(cases, ncpus)
    q30.require(tuple(code_state.generations) == generations and tuple(code_state.users) == users
                and bytes(code_state.roles) == roles and not any(users) and not any(roles)
                and not code_state.pending_generation and not any(code_state.pins.values()),
                "final code ownership/generation/metadata retirement disagreement")
    for cpu, state in enumerate(cpus):
        q30.require(state["started"] == state["done"] == 1 and state["depth"] == 0
                    and state["code_writes"] == code_state.writes[cpu]
                    and state["current_attempt"] == state["attempts"]
                    and state["timer_end"] > state["timer_start"] >= checkpoint_cpus[cpu]["timer_end"],
                    "Q31 final CPU completion/metadata lifetime/timer progress")
        q30.require(all(state[key] == value for key, value in counts[cpu].items()), "Q31 counters disagree with full metadata replay")
        final_case = cases[(12, 1, 15)]
        q30.require(state["arch"] == final_case["states"][cpu][-1]["arch"], "Q31 final architecture differs from last committed state")
        recipe = final_case["steps"][cpu][-1]["recipe"]
        attempt = final_case["attempts"][cpu][-1]
        expected_metadata = dict(identity=recipe["rip"], generation=recipe["generation"], rip=recipe["rip"],
                                 length=attempt["length"], instruction=recipe["instruction"], access=recipe["access"],
                                 width=recipe["width"], substep=recipe["substep"])
        q30.require(state["metadata"] == expected_metadata, "final native recovery metadata mismatch")
        host = [r for r in logs if r.get("cpu") == cpu and "q31_started" in r]
        q30.require(snapshot_only or len(host) == 1 and all(host[0]["q31_" + key] == state[key] for key in CPU[:10]),
                    "Q31 host/raw CPU summary disagreement")
        timers = [case["ends"][cpu]["timer"] for case in sorted(cases.values(), key=lambda c: c["serial"])]
        q30.require(state["timer_start"] <= timers[0] <= timers[-1] <= state["timer_end"]
                    and timers == sorted(timers), "Q31 case timer bounds/order")
    return dict(format="natemu-q31-checkpoint-check-v2" if snapshot_only else "natemu-q31-independent-check-v1", status="INCOMPLETE" if result["incomplete"] else "PASS",
                checkpoint=prefix_report, records=len(records), operations=len(result["operations"]), finite_cases=len(cases),
                checked=["actual nonterminal Q30 checkpoint and unchanged prefix", "full initial-to-final native paging replay",
                         "all 34 instruction/fault/frame/restart cases at 16 repetitions", "implicit supervisor IDT/TSS/frame/IRET accesses",
                         "coherent opcode bytes and precise GPR/flags/CR2 commits", "code ownership/generation/pin retirement",
                         "named guest CPUID and fresh native dispatch", "SMP revocation and concurrent fault recovery"],
                unsupported=sorted(result["incomplete"]))


def model_trace(path):
    """Exercise metadata semantics only; the model omits the native protocol."""
    with path.open() as stream:
        header = json.loads(next(stream))
        q30.require(header["format"] == "natemu-q31-model-trace-v1", "model trace format")
        records = [(s, k, c, e, tuple(d)) for s, k, c, e, d in map(json.loads, stream)]
    contexts = [q30.DEFAULT_CONTEXT] * header["ncpus"]
    pending, operations = {}, []
    for sequence, kind, cpu, epoch, data in records:
        if kind == q30.CONTEXT_EVENT:
            contexts[cpu] = data[:5]
        elif kind == q30.BEGIN:
            pending[cpu] = dict(sequence=sequence, cpu=cpu, begin=data, context=contexts[cpu])
        elif kind == q30.END:
            operation = pending.pop(cpu)
            q30.require(data[0] == operation["sequence"], "model operation linkage")
            operation.update(end_sequence=sequence, end=data)
            operations.append(operation)
    q30.require(not pending, "model pending operations")
    cases, counts = index_trace(records, operations, header["ncpus"], 0)
    finite_semantics(cases, header["ncpus"])
    return records, operations, cases, counts


def semantic_controls(cases, ncpus):
    """Corrupt actual decoded cases and exercise the same acceptance validators."""
    checked = []

    def reject(name, key, change):
        case = copy.deepcopy(cases[key])
        change(case)
        try:
            finite_semantics({key: case}, ncpus)
        except ValueError:
            checked.append(name)
            return
        raise AssertionError("semantic corruption accepted: " + name)

    def state(case, point, index=0):
        return [s for s in case["states"][0] if s["point"] & 255 == point][index]

    def step(case, action, index=0):
        return [s for s in case["steps"][0] if s["action"] == action][index]["operation"]

    def field(operation, field, index, value):
        row = list(operation[field]); row[index] = value; operation[field] = tuple(row)

    reject("scalar wrong destination register", (1, 0, 0), lambda c: state(c, 5)["arch"].update(rax=99))
    reject("RMW exposed uncommitted accumulator", (5, 1, 0), lambda c: state(c, 2)["arch"].update(rax=99))
    reject("RMW committed wrong arithmetic flags", (5, 0, 0),
           lambda c: state(c, 5, 1)["arch"].update(rflags=state(c, 5, 1)["arch"]["rflags"] ^ 1))
    reject("cross-page wrong fault byte", (6, 0, 0), lambda c: state(c, 2)["arch"].update(cr2=0x1000CFFC))
    def partial_store(case):
        operation = [s["operation"] for s in case["steps"][0]
                     if s["operation"]["begin"][0] == 0x10019FF8][1]
        field(operation, "end", 1, 0x556677889ABCDEF0)
    reject("denied split exposed first-half bytes", (6, 0, 0), partial_store)
    reject("REP wrong completed count", (7, 0, 0), lambda c: state(c, 6)["arch"].update(rcx=8))
    reject("REP skipped reverse direction", (7, 2, 0), lambda c: state(c, 6)["arch"].update(rdi=0x1000E004))
    reject("interrupt handler kept IF", (9, 0, 0), lambda c: state(c, 3)["arch"].update(rflags=0x202))
    reject("fault frame omitted saved RF", (9, 0, 0), lambda c: field(step(c, 4, 2), "begin", 3, 0x302))
    reject("privilege frame lost old SS", (9, 1, 0), lambda c: field(step(c, 4), "begin", 3, 0))
    reject("IRET invented resume PC", (9, 0, 0), lambda c: field(step(c, 5), "end", 1, 0x10003003))
    reject("DF nonzero error slot", (9, 7, 0), lambda c: field(step(c, 4, 5), "begin", 3, 1))

    def premature_transfer(case):
        state(case, 2, 1)["arch"].update(cs=8, ss=16, rsp=0x10005FC0)
        # Match the forged second-event frame to the forged early-transfer
        # snapshot: rejection must preserve the pre-transfer original baseline.
        pushes = [s["operation"] for s in case["steps"][0] if s["action"] == 4][:6]
        for operation, value in zip(pushes[:4], (16, 0x10005FC0, 0x10202, 8)):
            field(operation, "begin", 3, value)
    reject("contributory-to-PF premature architectural transfer", (9, 9, 0), premature_transfer)
    reject("nested handler contaminated outer CR2", (9, 10, 0), lambda c: state(c, 4, 1)["arch"].update(cr2=0x1000E000))
    reject("CPUID wrong leaf register", (10, 0, 0), lambda c: state(c, 5, 2)["arch"].update(rbx=0))
    reject("negative control omitted valid follow-up", (12, 0, 0), lambda c: c["attempts"][0].pop())
    def accepted_stale(case):
        index = next(i for i, (_, kind, data) in enumerate(case["events"][0]) if kind == MARK and data[0] == 6)
        seq, kind, data = case["events"][0][index]
        case["events"][0][index] = (seq, kind, (*data[:4], 0, *data[5:]))
    reject("stale dispatch generation accepted", (12, 1, 0), accepted_stale)
    return checked


def metadata_controls(records, operations, ncpus):
    rejected = []
    predicates = [
        ("foreign access generation", lambda r: r[1] == 13 and r[4][:2] == (MARK, 1), 7, 999),
        ("STEP wrong native status", lambda r: r[1] == 13 and r[4][0] == STEP, 3, 99),
        ("STEP orphan native operation", lambda r: r[1] == 13 and r[4][0] == STEP, 5, 0),
        ("foreign delivered attempt", lambda r: r[1] == 13 and r[4][0] == DELIVER, 1, 0),
        ("unknown control marker", lambda r: r[1] == 13 and r[4][:2] == (MARK, 4), 1, 99),
        ("mixed architectural snapshot chunks", lambda r: r[1] == 13 and r[4][0] == STATE_EVENT and r[4][4] == 1, 4, 3),
    ]
    for name, predicate, field, value in predicates:
        trace = list(records)
        index = next(i for i, r in enumerate(trace) if predicate(r))
        data = list(trace[index][4]); data[field] = value
        trace[index] = (*trace[index][:4], tuple(data))
        try:
            index_trace(trace, operations, ncpus, 0)
        except ValueError:
            rejected.append(name)
            continue
        raise AssertionError("metadata corruption accepted: " + name)
    return rejected


def code_controls(records, cases, ncpus):
    """Code-case controls use model bytes plus explicitly synthetic epoch bounds.

    This exercises the real code semantic predicate, not native protocol replay.
    The separate raw replay controls validate those protocol event rules.
    """
    code_state = CodeState()
    for sequence, kind, cpu, epoch, data in records:
        if kind != 13 or data[0] != CODE:
            continue
        data = list(data)
        data[5] *= 16
        action, page, old, new, operation, generation, users = data[1:]
        if action == 2 and operation:
            code_state.mutation(sequence * 16 - 1, cpu, page, q30.CODE, operation, 1, cpu, {})
        code_state.event(sequence * 16, kind, cpu, epoch, tuple(data), 1 if action in (1, 2) else 0, cpu, {})
    selected = {key: copy.deepcopy(case) for key, case in cases.items() if key in ((10, 0, 0), (11, 0, 0))}
    synthetic_epochs = []
    for (group, _, _), case in selected.items():
        case["start"] *= 16; case["stop"] *= 16
        for cpu in range(ncpus):
            for attempt in case["attempts"][cpu]:
                attempt["sequence"] *= 16
            for state in case["states"][cpu]:
                state["sequence"] *= 16; state["end_sequence"] *= 16
            for step in case["steps"][cpu]:
                step["sequence"] *= 16
                operation = step["operation"]
                operation["sequence"] *= 16; operation["end_sequence"] *= 16
                operation["end_walk"] = {"gpa": operation["end"][4]}
                operation["mutations"] = [e for e in code_state.generation_events if e[3] == operation["sequence"]]
            cpuid = next(a for a in case["attempts"][cpu] if a["instruction"] == 7)
            last = max(s["operation"]["end_sequence"] for s in case["steps"][cpu] if s["recipe"]["attempt"] == cpuid["identity"])
            target = 0x10000000 + cpu * 0x20000 + (0xC020 if group == 10 else 0x1B020)
            epoch = group * 4 + cpu
            for offset, kind, data in ((1, q30.GATE, (1,)), (2, q30.MARK, (1, target, 0x33)),
                                       (3, q30.GATE, (2,)), (4, q30.GATE, (0,))):
                synthetic_epochs.append((last + offset, kind, cpu, epoch, data + (0,) * (8 - len(data))))
    code_semantics(selected, code_state, ncpus, synthetic_epochs)
    rejected = []
    def reject(name, changed_cases=selected, changed_state=code_state, changed_epochs=synthetic_epochs):
        try:
            code_semantics(changed_cases, changed_state, ncpus, changed_epochs)
        except ValueError:
            rejected.append(name)
            return
        raise AssertionError("code semantic corruption accepted: " + name)
    reject("CPUID omitted actual invalidation", changed_epochs=[r for r in synthetic_epochs if r[1] != q30.MARK])
    reject("CPUID retired before gate completion", changed_epochs=[r for r in synthetic_epochs if r[1] != q30.GATE or r[4][0] != 0])
    bad = copy.deepcopy(code_state)
    interval = next(p for p in bad.pin_intervals if p["cpu"] == 1 and p["page"] == 0x28C000
                    and selected[(11, 0, 0)]["start"] < p["start"] < selected[(11, 0, 0)]["stop"])
    interval["generation"] += 1
    reject("dispatch pinned wrong generation", changed_state=bad)
    bad = copy.deepcopy(selected)
    operation = next(s["operation"] for s in bad[(10, 0, 0)]["steps"][0] if s["operation"]["begin"][1] == 3)
    operation["end_walk"]["gpa"] = 0x5000
    reject("native dispatch escaped decoded guest immediate", changed_cases=bad)
    bad = copy.deepcopy(code_state)
    case = selected[(11, 0, 0)]
    attempt = case["attempts"][1][1]
    interval = next(p for p in bad.pin_intervals if p["cpu"] == 1 and p["start"] < attempt["sequence"] < p["stop"])
    interval["stop"] = attempt["sequence"] + 1
    reject("remote recovery metadata freed before fault", changed_state=bad)
    return rejected


def self_check():
    def rejects(call):
        try:
            call()
        except ValueError:
            return
        raise AssertionError("Q31 corrupt synthetic control accepted")

    old = dict(error=0, rip=0x10003000, cs=8, rsp=0x10005FF9, ss=16, rflags=0x302)
    frame = fault_frame(old, old["rsp"], 8, 14, False)
    assert frame == dict(rsp=0x10005FC0, slots=(0, old["rip"], 8, 0x10302, old["rsp"], 16),
                         cs=8, ss=16, rflags=2)
    assert fault_frame(old, 0x10009FFB, 8, 14, True)["ss"] == 0
    assert fault_frame(old, 0x10007FF7, 8, 14, True)["rsp"] == 0x10007FC0
    assert fault_frame(old, old["rsp"], 8, 15, False)["rflags"] == 0x202
    rf = dict(old, rflags=0x10302)
    assert fault_frame(rf, rf["rsp"], 8, 14, False) == frame
    assert delivery_pair(14, 14) == delivery_pair(14, 11) == 8
    assert delivery_pair(13, 14) == 14 and delivery_pair(8, 14) == "shutdown"
    trace = [(index + 1, 13, 0, 1, (STATE_EVENT, 1, 1, 1, index, 3 * index, 3 * index + 1, 3 * index + 2))
             for index in range(7)]
    assert list(snapshots(trace)[0]["arch"].values()) == list(range(21))
    rejects(lambda: snapshots(trace[:-1]))
    rejects(lambda: snapshots(trace[:2] + [trace[3], trace[2]] + trace[4:]))
    rejects(lambda: snapshots(trace + trace))

    # A real nonterminal envelope has no terminal flag, end-ticks or completion
    # event. Build separate synthetic bytes; never rewrite a retained run.
    ram = bytearray(q30.RAM_BYTES_V3)
    sym = {"_l1": 0x40010000, "_l2": 0x40011000, "_device_l2": 0x40012000,
           "_aliases": 0x40013000, "_pages": 0x40014000, "_fault_site": 0x40000800}
    struct.pack_into("<6Q", ram, 0x800000, 0x4E4154454D553031, 1, 2, 1, 27, 24000000)
    q30.put(ram, 0x900000, 0x13579BDF2468ACE0)
    cpus = []
    for cpu in range(2):
        saved = dict.fromkeys(q30.FOUNDATION_FIELDS, 0)
        saved.update(ready=1, tso_start=2, sctlr=0x1005, tcr=0x1B5103510, sync_count=1,
                     sgi_count=1, timer_count=3, timer_intid=27, alias_value=0x13579BDF2468ACE0,
                     esr=0x96000007, far=0x60001000, elr=sym["_fault_site"], trace_count=6)
        events = [(1, saved["alias_value"], 0), (2, saved["esr"], 0), (3, 1, 0)] + [(3, 27, 0)] * 3
        snapshot = tuple(saved.values()) + tuple(value for event in events for value in event) + (0,) * (3 * 26)
        pool = 0x40200000 + cpu * 0x80000
        cpus.append(dict(foundation=snapshot, timer_start=3, timer_end=4, final_tcr=0x1B5103510,
                         final_ttbr0=pool | ((cpu + 1) << 48), final_ttbr1=pool + 4096))
        live = dict(saved, timer_count=5, trace_count=8)
        struct.pack_into("<22Q", ram, 0x800000 + 48 + cpu * 944, *live.values())
        for index, event in enumerate(events + [(3, 27, 0)] * 2):
            struct.pack_into("<3Q", ram, 0x800000 + 48 + cpu * 944 + 176 + index * 24, *event)
        q30.put(ram, pool - 0x40000000, sym["_l1"] | 3)
    entries = {"_l1": {0: sym["_device_l2"] | 3, 1: sym["_l2"] | 3},
               "_l2": {**{i: (sym["_pages"] + i * 4096) | 3 for i in range(256)}, 256: sym["_aliases"] | 3},
               "_aliases": {0: 0x40900703},
               "_device_l2": {128: 0x10000000 | 0x405 | (3 << 53),
                              **{136 + i: (0x11000000 + i * 0x200000) | 0x405 | (3 << 53) for i in range(8)}}}
    for name, values in entries.items():
        for index, value in values.items():
            q30.put(ram, sym[name] - 0x40000000 + 8 * index, value)
    for index in range(131072):
        q30.put(ram, sym["_pages"] - 0x40000000 + 8 * index, (0x40000000 + 4096 * index) | 0x703)
    q30.foundation(ram, cpus, sym, terminal=False, version=3)
    rejects(lambda: q30.foundation(ram, cpus, sym, version=3))
    for field in ("done", "tso_end", "ticks_end"):
        address = 0x800000 + 48 + q30.FOUNDATION_FIELDS.index(field) * 8
        q30.put(ram, address, 1)
        rejects(lambda: q30.foundation(ram, cpus, sym, terminal=False, version=3))
        q30.put(ram, address, 0)
    q30.put(ram, 0x800000 + 48 + 176 + 7 * 24, 4)
    rejects(lambda: q30.foundation(ram, cpus, sym, terminal=False, version=3))

    memory = bytearray(q30.RAM_SIZE)
    for address, value in ((0x200000, 0x201027), (0x201000, 0x202027),
                           (0x202000, 0x203027), (0x203000, 0x100067), (0x100000, 1234)):
        q30.put(memory, address, value)
    events, epoch = [], 1

    def emit(kind, cpu, *data):
        sequence = len(events) + 1
        events.append((sequence, kind, cpu, epoch, tuple(data) + (0,) * (8 - len(data))))
        return sequence

    def code(action, cpu, old, new, op, generation, users):
        emit(13, cpu, CODE, action, 0x100000, old, new, op, generation, users)

    emit(q30.GATE, 0, 1, 0)
    for address, role in ((0x200000, 8), (0x201000, 4), (0x202000, 2), (0x203000, 1)):
        emit(q30.ROLE, 0, address, 0, role)
    emit(q30.CONTEXT_EVENT, 0, *q30.DEFAULT_CONTEXT)
    code(1, 0, 0, 1, 0, 0, 0)
    code(2, 0, 0, 1, 0, 1, 0)
    emit(q30.GATE, 0, 2, 0)
    emit(q30.ACK, 0, 1, 0, 0)
    emit(q30.ACK, 1, 1, 4, 0)
    emit(q30.GATE, 0, 0, 0)
    code(3, 1, 0, 1, 0, 1, 1)
    reader = emit(q30.BEGIN, 1, 0, 0, 8, 0, 0, 0, 0x202, 1)
    emit(q30.MARK, 1, 6, 1, reader)
    emit(q30.FAULT, 1, reader, 0x40000800, 1 << 47, 0x96000007)
    emit(q30.MARK, 1, 6, 0, reader)
    emit(q30.MAP, 1, 0, 0x100000, 0x40284000, 0x40B00F83 | (1 << 54), 4096, 15, 0, 0)
    emit(q30.MARK, 1, 6, 1, reader)
    emit(q30.END, 1, reader, 1234, 0, 0, 0x100000, 0, 0x202, 1)
    emit(q30.MARK, 1, 6, 0, reader)
    writer = emit(q30.BEGIN, 0, 0, 1, 8, 4321, 0, 0, 0x202, 1)
    emit(q30.MARK, 0, 6, 1, writer)
    emit(q30.FAULT, 0, writer, 0x40000804, 1 << 47, 0x96000047)
    emit(q30.MARK, 0, 6, 0, writer)
    epoch = 2
    emit(q30.GATE, 0, 1, 0)
    emit(q30.MUTATION, 0, 0x100000, 1234, 4321, 8, q30.CODE, writer, 0)
    code(2, 0, 1, 2, writer, 2, 1)
    emit(q30.GATE, 0, 2, 0)
    emit(q30.ACK, 0, 2, 0, 0)
    emit(q30.ACK, 1, 2, 4, 0, 0, reader, 1)
    emit(q30.GATE, 0, 0, 0)
    emit(q30.MARK, 0, 6, 1, writer)
    emit(q30.END, 0, writer, 4321, 0, 0, 0x100000, 0, 0x202, 1)
    emit(q30.MARK, 0, 6, 0, writer)
    code(4, 1, 1, 0, 0, 2, 0)
    code_state = CodeState()
    replay = q30.replay(memory, events, 2, version=3, code_state=code_state)
    assert not replay["incomplete"] and q30.word(replay["memory"], 0x100000) == 4321
    assert code_state.generations[256] == 2 and code_state.users[256] == 0
    rejects(lambda: q30.replay(memory, events, 2))
    rejects(lambda: q30.replay(memory, events, 2, version=3))

    def changed(index, field, value):
        trace = list(events)
        data = list(trace[index][4]); data[field] = value
        trace[index] = (*trace[index][:4], tuple(data))
        return trace

    generation = next(i for i, r in enumerate(events) if r[1] == 13 and r[4][:2] == (CODE, 2) and r[4][5] == writer)
    bad = [r for i, r in enumerate(events) if i != generation]
    rejects(lambda: q30.replay(memory, bad, 2, version=3, code_state=CodeState()))
    for index, field, value in ((generation, 4, 1), (generation, 5, reader),
                                 (next(i for i, r in enumerate(events) if r[1] == q30.MAP), 6, 1)):
        bad = changed(index, field, value)
        rejects(lambda: q30.replay(memory, bad, 2, version=3, code_state=CodeState()))
    bad = list(events)
    bad[-1] = (*bad[-1][:2], 0, *bad[-1][3:])
    rejects(lambda: q30.replay(memory, bad, 2, version=3, code_state=CodeState()))
    mixed_memory = bytearray(memory)
    q30.put(mixed_memory, 0x203000, 0x203067)
    mixed = []
    for record in events:
        data = list(record[4])
        if record[1] == 13 and data[0] == CODE:
            data[2] = 0x203000
        elif record[1] == q30.MAP:
            data[1] = 0x203000; data[3] += 0x103000; data[7] = 1
        elif record[1] == q30.BEGIN and record[0] == writer:
            data[3] = 0x203067 | (1 << 63)
        elif record[1] == q30.MUTATION:
            data[:3] = [0x203000, 0x203067, 0x203067 | (1 << 63)]; data[4] = q30.GUEST
        elif record[1] == q30.END:
            data[1] = 0x203067 | ((1 << 63) if data[0] == writer else 0); data[4] = 0x203000
        mixed.append((*record[:4], tuple(data)))
    mixed_code = CodeState()
    outcome = q30.replay(mixed_memory, mixed, 2, version=3, code_state=mixed_code)
    assert not outcome["incomplete"] and outcome["counts"][0]["table_traps"] == 1 and mixed_code.writes[0] == 1
    rejects(lambda: q30.replay(mixed_memory, [r for r in mixed if not (r[1] == 13 and r[4][:2] == (CODE, 2)
                                                                   and r[4][5] == writer)],
                              2, version=3, code_state=CodeState()))
    for kind in (0, 4):
        trace = list(events)
        position = next(i for i, r in enumerate(trace) if r[0] == reader)
        data = list(trace[position][4]); data[1:3] = [kind, 1]
        trace[position] = (*trace[position][:4], tuple(data))
        position = next(i for i, r in enumerate(trace) if r[1] == q30.END and r[4][0] == reader)
        data = list(trace[position][4]); data[1] = 1234 & 255
        trace[position] = (*trace[position][:4], tuple(data))
        assert not q30.replay(memory, trace, 2, version=3, code_state=CodeState())["incomplete"]
        fault_symbols = dict(_q30_load8_site=0x40000800, _q30_load_site=0x40000820,
                             _q30_store_site=0x40000804, _q30_byte_site=0x40000824,
                             _q30_cas_load=0x40000828, _q30_cas_store=0x4000082C)
        q30.validate_native_faults(trace, fault_symbols)
        rejects(lambda: q30.validate_native_faults(trace, dict(fault_symbols, _q30_load8_site=0x40000830)))
        rejects(lambda: q30.replay(memory, trace, 2))
    opcode_memory = bytearray(memory)
    opcode_memory[0x100000:0x100003] = b"\x48\x8b\x07"
    observer = CodeState(); observer.bind(opcode_memory, [q30.DEFAULT_CONTEXT] * 2)
    attempt = (ATTEMPT, 1, 0, 0, 1, 3, 8, 1)
    observer.event(1, 13, 0, 0, attempt, 0, None, {})
    opcode_memory[0x100001] = 0x89
    rejects(lambda: observer.event(1, 13, 0, 0, attempt, 0, None, {}))
    opcode_memory[0x100001] = 0x8B
    q30.put(opcode_memory, 0x201000, q30.word(opcode_memory, 0x201000) | (1 << 63))
    rejects(lambda: observer.event(1, 13, 0, 0, attempt, 0, None, {}))
    overlap = {(6, 2, 0): {"steps": [[] for _ in range(4)]}, (8, 0, 0): {"steps": [[] for _ in range(4)]}}
    def observed(group, cpu, start, stop, action, address, value, fault_count, status):
        operation = dict(cpu=cpu, sequence=start, end_sequence=stop, begin=(address, 1 if action in (2, 6) else 0, 8, value),
                         end=(start, 0, status, 3 if status else 0), faults=fault_count)
        overlap[(group, 2 if group == 6 else 0, 0)]["steps"][cpu].append(dict(action=action, operation=operation))
    observed(6, 1, 10, 30, 2, 0x1002CFFC, 0x1122334455667788, 0, 14)
    observed(6, 0, 15, 25, 6, 0x1001A168, 0x2AD005, 1, 0)
    observed(8, 0, 40, 60, 1, 0x1000D000, 0, 1, 14)
    observed(8, 1, 50, 70, 2, 0x1002D000, 99, 1, 14)
    concurrent_semantics(overlap, 4)
    for group, cpu, key, value in ((6, 0, "sequence", 31), (8, 1, "sequence", 61), (6, 0, "faults", 0)):
        bad = copy.deepcopy(overlap)
        bad[(group, 2 if group == 6 else 0, 0)]["steps"][cpu][0]["operation"][key] = value
        rejects(lambda: concurrent_semantics(bad, 4))
    bad = copy.deepcopy(overlap)
    operation = bad[(6, 2, 0)]["steps"][0][0]["operation"]
    operation["begin"] = (*operation["begin"][:3], 0x2CD005)
    rejects(lambda: concurrent_semantics(bad, 4))
    print("Q31 self-check PASS: architecture, snapshots, nonterminal foundation, byte/fetch native replay, opcode permissions, code generation and SMP coverage controls; no VM or raw acceptance run")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", nargs="?", type=Path)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--model-trace", type=Path, help="metadata semantics only; never raw acceptance")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if args.model_trace:
        records, operations, cases, counts = model_trace(args.model_trace)
        controls = (semantic_controls(cases, len(counts)) + code_controls(records, cases, len(counts))
                    + metadata_controls(records, operations, len(counts)))
        print(json.dumps(dict(status="SYNTHETIC SEMANTICS PASS", cases=len(cases), operations=len(operations),
                              corruptions_rejected=controls,
                              unsupported=["native paging/ACK/A-D replay", "actual concurrency", "raw envelope"], vm=False)))
        return
    if args.run is None:
        parser.error("run directory required")
    try:
        report = q30.check_run(args.run, continuation=check)
    except (ValueError, OSError, KeyError, TypeError, struct.error) as error:
        print(json.dumps(dict(status="FAIL", error=str(error)), sort_keys=True))
        raise SystemExit(1)
    print(json.dumps(report, indent=2, sort_keys=True))
    raise SystemExit(0 if report["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()

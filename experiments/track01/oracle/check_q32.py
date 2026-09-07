#!/usr/bin/env python3
"""Independent Q32 interrupt oracle; preparation and controls never execute a VM."""
import argparse
import copy
import json
from pathlib import Path
import struct

import check_q30 as q30
import check_q31 as q31

STATE = 0xE10000
MAGIC = 0x5133324952515354
HEADER = "magic version ncpus failure phase cases checkpoint_records checkpoint_epoch spi_base spi_count sgi eoimode".split()
CPU_PREFIX = "started done cases native_count guest_count boundaries shadow tpr halted stage ready wake_generation".split()
CPU_SUFFIX = ("spi_request spi_sent spi_deassert spi_deasserted spi_level hook_stage hook_source hook_target hook_vector "
              "last_elr last_spsr last_stage interior_count handler_effects timer_start timer_end").split()
CPU_WORDS = 85
STATE_BYTES = 96 + 4 * CPU_WORDS * 8
CASE, EMIT, NATIVE, ACCEPT, BOUNDARY, DISPATCH, EOI, SHADOW, HALT, CONTROL, END = range(1, 12)
TIMER, PEER, HOST = 1, 2, 3
VARIANTS = dict(enumerate((3, 3, 3, 6, 3, 2, 4, 3, 3, 3, 2, 2), 1))


class CodeState(q31.CodeState):
    q32 = True

    def event(self, sequence, kind, cpu, epoch, data, phase, owner, active):
        if kind == 14:
            q30.require(data[0] in range(1, 12), "unknown Q32 metadata event")
            return False
        exclusive = super().event(sequence, kind, cpu, epoch, data, phase, owner, active)
        if kind == 13 and data[0] == q31.ATTEMPT and data[4] > 7:
            _, attempt, identity, rip, instruction, length, width, generation = data
            encoding = {8: b"\xfa", 9: b"\xfb", 10: b"\x8e\xd0", 11: b"\xf4", 12: b"\x90",
                        14: b"\x48\x89\x07", 15: b"\x48\x89\xc4", 16: b"\xeb"}.get(instruction)
            q30.require(encoding is not None and (len(encoding) == length or instruction == 16 and length == 2), "unimplemented Q32 native instruction recipe")
            context = dict(zip(q30.CONTEXT, self.contexts[cpu])); context.pop("pge")
            actual = bytearray()
            for address in range(rip, rip + len(encoding)):
                result = q30.walk(self.memory, gva=address, access="execute", **context)
                q30.require(result["status"] == "ok", "Q32 instruction metadata has no executable guest bytes")
                actual.append(self.memory[result["gpa"]])
            q30.require(actual == encoding, "Q32 instruction metadata contradicts actual guest opcode")
        return exclusive


def apic_trace(records, ncpus, timer_intid, spi_base, symbols, prefix_records, finite_cases=True,
               variant_counts=None, initial_states=None, scope_end_counters=True, max_native_stage=6, broadcast_groups=(11,)):
    """Independent APIC transitions and native notification/payload accounting."""
    cpus = [dict(irr=set(), isr=set(), tpr=0, shadow=0, halted=0, native_count=0, guest_count=0,
                 boundaries=0, wake_generation=0, pending_dispatch=None, emitted={}, accepted={}, vectors={},
                 config=None, diagnostic=None, diagnostic_count=0, sgi_sent=[0] * 4, raw_irq=[0, 0, 0], empty_irq=[0, 0, 0], quiet=None, native=[], deliveries=[], shadow_events=[], halt_events=[], controls=[])
            for _ in range(ncpus)]
    variants = VARIANTS if variant_counts is None else variant_counts
    if initial_states is not None:
        q30.require(len(initial_states) == ncpus, "continued APIC initial CPU count")
        for state, initial in zip(cpus, initial_states):
            for name in ("native_count", "guest_count", "boundaries", "wake_generation"):
                state[name] = initial[name]
            state["irr"], state["isr"] = bitmap(initial["irr"]), bitmap(initial["isr"])
            for name in ("emitted", "accepted", "vectors"):
                state[name] = {(source + 1, sender): value for source, row in enumerate(initial[name])
                               for sender, value in enumerate(row) if value}
            for name in ("raw_irq", "empty_irq", "sgi_sent"):
                state[name] = list(initial[name])
            state["diagnostic_count"] = initial["diagnostic_ack"]
    cases, active_case = {}, None
    for sequence, kind, cpu, epoch, data in records:
        if sequence <= prefix_records or kind != 14:
            continue
        q30.require(cpu < ncpus and data[0] in range(1, 12), "unknown Q32 event/CPU")
        subtype, payload = data[0], data[1:]
        state = cpus[cpu]
        if subtype == CASE:
            group, variant, rep, start, serial, target, role = payload
            key = group, variant, rep
            q30.require(cpu == 0 and group in variants and variant < variants[group] and rep < 16
                        and start in (0, 1) and target == q30.MASK and role == 0, "Q32 finite case identity")
            if start:
                q30.require(active_case is None and key not in cases and serial == len(cases) + 1,
                            "nested/duplicate Q32 case")
                for own in cpus:
                    q30.require(not own["irr"] and not own["isr"] and own["pending_dispatch"] is None,
                                "Q32 case abandoned pending/in-service interrupt")
                    own.update(tpr=0, shadow=0, halted=0)
                cases[key] = dict(start=sequence, stop=None, serial=serial, target=target, role=role,
                                  events=[[] for _ in range(ncpus)], ends={})
                active_case = key
            else:
                q30.require(active_case == key and cases[key]["serial"] == serial
                            and set(cases[key]["ends"]) == set(range(ncpus)), "incomplete Q32 case")
                cases[key]["stop"] = sequence
                active_case = None
            continue
        if subtype == CONTROL and payload[0] == 11:
            _, ctlr, timer, spi, sgi, redist, mpidr = payload
            q30.require(active_case is None and state["config"] is None and ctlr & 2 == 0
                        and (timer, spi, sgi) == (timer_intid, spi_base + cpu, 2) and mpidr & 255 == cpu,
                        "native fixed interrupt configuration or EOImode mismatch")
            state["config"] = payload
            continue
        if subtype == CONTROL and payload[0] == 8:
            _, source, intid, raw, empty, stage, deasserted = payload
            q30.require(source in (PEER, HOST) and intid == (2 if source == PEER else spi_base + cpu)
                        and raw == state["raw_irq"][source - 1] + 1
                        and empty == state["empty_irq"][source - 1] + 1 and stage <= max_native_stage,
                        "unaccounted empty native acknowledgement")
            state["raw_irq"][source - 1], state["empty_irq"][source - 1] = raw, empty
            if active_case is not None:
                cases[active_case]["events"][cpu].append((sequence, subtype, payload))
            continue
        q30.require(active_case is not None, "Q32 event outside its declared finite case")
        case = cases[active_case]
        case["events"][cpu].append((sequence, subtype, payload))
        if subtype == EMIT:
            source, sender, target, emitted, vector, route, flags = payload
            q30.require(source in (1, 2, 3) and sender == cpu and target < ncpus and 32 <= vector <= 255
                        and route in (0, 1) and flags == int(active_case[0] in broadcast_groups)
                        and (source == PEER or target == cpu and route == 0),
                        "invalid native source/target publication")
            receiver, key = cpus[target], (source, sender)
            previous = receiver["emitted"].get(key, 0)
            q30.require(emitted == previous + 1 and (previous == receiver["accepted"].get(key, 0)
                        or receiver["vectors"][key] == vector), "lost/reordered native payload sequence")
            receiver["emitted"][key] = emitted
            receiver["vectors"][key] = vector
        elif subtype == NATIVE:
            source, intid, elr, spsr, stage, acknowledgement, target = payload
            q30.require(source in (1, 2, 3) and intid == {TIMER: timer_intid, PEER: 2, HOST: spi_base + cpu}[source]
                        and target == cpu and acknowledgement == state["native_count"] + 1 and stage <= max_native_stage,
                        "native source/INTID/target/acknowledgement mismatch")
            q30.require(spsr & 15 == 5 and spsr & 128 == 0, "native arrival did not interrupt unmasked EL1h state")
            q30.require(symbols["_start"] <= elr < symbols["_text_end"], "native IRQ ELR outside retained executable text")
            q30.require(any(source == key[0] and count > state["accepted"].get(key, 0)
                            for key, count in state["emitted"].items()), "native notification without published source payload")
            state["raw_irq"][source - 1] += 1
            state["native_count"] = acknowledgement
            state["wake_generation"] += 1
            state["native"].append((sequence, payload))
        elif subtype == ACCEPT:
            source, sender, previous, accepted, vector, old, acknowledgement = payload
            key = source, sender
            q30.require(state["native"] and state["native"][-1][1][0] == source
                        and acknowledgement == state["native_count"] and sender < ncpus
                        and previous == state["accepted"].get(key, 0) < accepted <= state["emitted"].get(key, 0)
                        and vector == state["vectors"][key] and old == int(vector in state["irr"]),
                        "native payload/IRR acceptance mismatch")
            state["accepted"][key] = accepted
            state["irr"].add(vector)
        elif subtype == BOUNDARY:
            boundary, rip, flags, shadow, tpr, vector, halted = payload
            q30.require(boundary == state["boundaries"] + 1 and shadow == state["shadow"] and tpr == state["tpr"]
                        and halted == state["halted"] and vector == eligible(state["irr"], state["isr"], tpr, flags, shadow)
                        and state["pending_dispatch"] is None, "wrong guest boundary eligibility or interrupt priority")
            state["boundaries"] = boundary
            if vector:
                state["irr"].remove(vector); state["isr"].add(vector)
                state["halted"] = state["shadow"] = 0
                state["pending_dispatch"] = (sequence, payload)
        elif subtype == DISPATCH:
            boundary, vector, rsp, depth, error_word, acknowledgement, generation = payload
            pending = state["pending_dispatch"]
            q30.require(pending is not None and (boundary, vector) == (pending[1][0], pending[1][5])
                        and acknowledgement == state["native_count"] and generation > 0 and depth > 0
                        and error_word in (0, 1), "early/duplicate/foreign guest dispatch")
            state["guest_count"] += error_word == 0
            state["deliveries"].append((pending, sequence, payload))
            state["pending_dispatch"] = None
        elif subtype == EOI:
            vector, highest, tpr, boundary, *reserved = payload
            q30.require(state["isr"] and vector == max(state["isr"]) and tpr == state["tpr"]
                        and boundary == state["boundaries"] and not any(reserved), "guest EOI cleared wrong ISR priority")
            state["isr"].remove(vector)
            q30.require(highest == max(state["isr"], default=0), "guest EOI ISR summary disagreement")
        elif subtype == SHADOW:
            instruction, old_if, new_if, old_shadow, new_shadow, boundary, exception = payload
            q30.require(instruction in (range(1, 16) if exception else range(8, 16)) and old_if in (0, 1) and new_if in (0, 1)
                        and old_shadow == state["shadow"] and boundary == state["boundaries"]
                        and exception in (0, 1), "invalid interrupt-shadow transition")
            expected = 0 if exception else int(instruction == 10 or instruction == 9 and not old_if)
            q30.require(new_shadow == expected and new_if == (0 if exception else 0 if instruction == 8 else
                        1 if instruction == 9 else old_if), "STI/MOV SS/CLI interrupt inhibition mismatch")
            state["shadow"] = new_shadow
            if instruction == 11 and not exception:
                state["halted"] = 1
            state["shadow_events"].append((sequence, payload))
        elif subtype == HALT:
            action, wake, selected, interrupt_flag, rip, native, stage = payload
            q30.require(action in (1, 2, 3) and state["halted"] == 1 and interrupt_flag in (0, 1)
                        and wake == state["wake_generation"] and native == state["native_count"]
                        and selected == eligible(state["irr"], state["isr"], state["tpr"], interrupt_flag << 9, state["shadow"]),
                        "halt publication/recheck/native-wake state mismatch")
            state["halt_events"].append((sequence, payload))
        elif subtype == CONTROL:
            if payload[0] == 1:
                _, vector, old, boundary, *reserved = payload
                q30.require(32 <= vector <= 255 and old == int(vector in state["irr"])
                            and boundary == state["boundaries"] and not any(reserved), "software pending input mismatch")
                state["irr"].add(vector)
            elif payload[0] == 2:
                _, halted, flags, rip, native, request, acknowledgement = payload
                q30.require(active_case[:2] == (7, 3) and halted == state["halted"] == 1
                            and flags & 512 == 0 and native == state["native_count"]
                            and state["diagnostic"] is not None and request == acknowledgement == state["diagnostic"][1]
                            and state["irr"] == {0x41} and not state["isr"], "masked HLT diagnostic invented a guest wake")
                state["irr"].clear(); state["halted"] = 0
                state["diagnostic"] = None
            elif payload[0] == 3:
                _, vector, fault, cr2, rsp, depth, zero = payload
                q30.require(active_case[:2] == (9, 2) and vector == 0x41 and fault == 14 and depth == 1 and zero == 0
                            and state["isr"] == {0x41} and not state["irr"]
                            and cr2 == 0x10011008 + cpu * 0x20000,
                            "invalid explicit interrupt-delivery-fault context stop")
                state["isr"].clear()
            elif payload[0] == 4:
                _, old, new, boundary, *reserved = payload
                q30.require(old == state["tpr"] and new in (0, 0x50) and boundary == state["boundaries"]
                            and not any(reserved), "TPR transition lacks exact prior state")
                state["tpr"] = new
            elif payload[0] == 5:
                _, minimum, actual, *reserved = payload
                q30.require(ncpus == actual == 2 and not any(reserved) and
                            (active_case[:2] == (6, 0) and minimum == 3 or active_case[0] == 11 and minimum == 4),
                            "unsupported applicability exclusion")
            elif payload[0] == 6:
                _, one, two, first, second, zero1, zero2 = payload
                q30.require(active_case[:2] == (6, 0) and ncpus == 4 and one != two and one != cpu and two != cpu
                            and first == state["emitted"].get((PEER, one), 0) > state["accepted"].get((PEER, one), 0)
                            and second == state["emitted"].get((PEER, two), 0) > state["accepted"].get((PEER, two), 0)
                            and state["sgi_sent"][one] == first and state["sgi_sent"][two] == second
                            and zero1 == zero2 == 0, "collapsed native notification lacks two unpublished payloads")
            elif payload[0] == 7:
                _, leader, route, mask, word, self_word, count = payload
                q30.require(active_case[0] == 11 and ncpus == count == 4 and leader == cpu == active_case[2] % 4
                            and route == active_case[1] and mask == (15 & ~(1 << leader))
                            and word == (2 << 24 | (1 << 40 if route else mask))
                            and self_word == (2 << 24 | 1 << leader), "broadcast SGI routing is not declared target set")
            elif payload[0] == 15:
                _, ordinal, elr, wanted, qualified, rip, count = payload
                q30.require(active_case[:2] == (7, 2) and 1 <= ordinal <= 8 and qualified in (0, 1)
                            and count == state["native_count"] and state["halted"] == 1
                            and state["irr"] == {0x41} and not state["isr"], "HLT trial lost unserviced native wake state")
            elif payload[0] == 10:
                _, source, raw, tick, deasserted, z1, z2 = payload
                q30.require(active_case[:2] == (6, 1) and source == HOST and raw == state["raw_irq"][2]
                            and deasserted > 0 and z1 == z2 == 0 and state["quiet"] is None,
                            "level IRQ quiet window did not start after deassertion")
                state["quiet"] = (raw, tick, deasserted)
            elif payload[0] == 9:
                _, source, before, after, start, end, deasserted = payload
                quiet = state["quiet"]
                q30.require(active_case[:2] == (6, 1) and source == HOST and quiet is not None
                            and before == after == quiet[0] == state["raw_irq"][2]
                            and start < quiet[1] and end >= quiet[1] + 2 and deasserted == quiet[2],
                            "level IRQ reasserted during the measured deasserted quiet window")
                state["quiet"] = None
            elif payload[0] == 14:
                _, request, serial, flags, rip, irr, halted = payload
                q30.require(active_case[:2] == (7, 3) and request == state["diagnostic_count"] + 1
                            and serial == case["serial"] and flags & 512 == 0 and irr == 2 and halted == 1
                            and state["halted"] == 1 and state["irr"] == {0x41} and state["diagnostic"] is None,
                            "external masked-HLT diagnostic request lacks actual retained halted state")
                state["diagnostic_count"] = request
                state["diagnostic"] = payload
            elif payload[0] == 13:
                _, sender, target, sent, word, route, zero = payload
                q30.require(sender == cpu and target < ncpus and route in (0, 1) and zero == 0
                            and sent == cpus[target]["emitted"].get((PEER, sender), 0)
                            and sent > cpus[target]["sgi_sent"][sender]
                            and word == (2 << 24 | ((1 << sender if target == sender else
                                1 << 40 if route else 15 & ~(1 << sender)) if active_case[0] == 11 else
                                (1 << 40 if route else 1 << target))),
                            "physical SGI send lacks matching target payload and instruction operand")
                cpus[target]["sgi_sent"][sender] = sent
            elif payload[0] == 12:
                _, partial, rip, rcx, rsi, rdi, boundary = payload
                q30.require(active_case[0] == 10 and partial == 1 and rcx > 0
                            and state["pending_dispatch"] is not None and boundary == state["boundaries"],
                            "partial REP interrupt control outside a selected boundary")
            else:
                # Additional producer control actions must receive independent
                # semantics before raw acceptance is enabled.
                q30.require(False, "Q32 control action is not implemented: " + str(payload[0]))
            state["controls"].append((sequence, payload))
        elif subtype == END:
            serial, group, variant, rep, deliveries, native, boundaries = payload
            q30.require((group, variant, rep) == active_case and serial == case["serial"] and cpu not in case["ends"]
                        and (not scope_end_counters or (deliveries, native, boundaries) == (state["guest_count"], state["native_count"], state["boundaries"])),
                        "Q32 per-case counters or completion mismatch")
            case["ends"][cpu] = sequence
    required = {(group, variant, rep) for group, count in variants.items() for variant in range(count) for rep in range(16)}
    q30.require(active_case is None and (set(cases) == required if finite_cases else bool(cases)), "missing Q32 finite cases")
    return cases, cpus


def decode(ram, ncpus, required_version=None):
    header = dict(zip(HEADER, struct.unpack_from("<12Q", ram, STATE)))
    q30.require((header["magic"], header["version"], header["ncpus"], header["failure"])
                == (MAGIC, header["version"], ncpus, 0) and header["version"] in (1, 2)
                and (required_version is None or header["version"] == required_version), "Q32 identity/version/failure")
    q30.require(header["sgi"] == 2 and header["eoimode"] == 0 and header["spi_base"] >= 32
                and header["spi_count"] >= ncpus and header["spi_base"] + header["spi_count"] <= 1020,
                "Q32 native interrupt configuration")
    cpus = []
    for cpu in range(4):
        words = struct.unpack_from(f"<{CPU_WORDS}Q", ram, STATE + 96 + cpu * CPU_WORDS * 8)
        if cpu >= ncpus:
            q30.require(not any(words), "unused Q32 CPU slot")
            continue
        state = dict(zip(CPU_PREFIX, words[:12]))
        state["irr"], state["isr"] = words[12:16], words[16:20]
        for name, offset in (("emitted", 20), ("accepted", 32), ("vectors", 44)):
            state[name] = tuple(tuple(words[offset + 4 * source:offset + 4 * source + 4]) for source in range(3))
        state.update(zip(CPU_SUFFIX, words[56:72]))
        state["raw_irq"], state["empty_irq"] = words[72:75], words[75:78]
        state["sgi_sent"] = words[78:82]
        state.update(zip(("diagnostic_request", "diagnostic_ack", "diagnostic_case"), words[82:85]))
        cpus.append(state)
    return header, cpus


def bitmap(words):
    return {index * 64 + bit for index, word in enumerate(words) for bit in range(64) if word & (1 << bit)}


def eligible(irr, isr, tpr, rflags, shadow):
    q30.require(0 <= tpr <= 255 and shadow in (0, 1), "invalid guest priority/shadow")
    q30.require(all(32 <= vector <= 255 for vector in irr | isr), "invalid fixed APIC vector")
    if not rflags & (1 << 9) or shadow:
        return 0
    threshold = max(tpr >> 4, max(isr, default=0) >> 4)
    return max((vector for vector in irr if vector >> 4 > threshold), default=0)


def interrupt_frame(old, selected_rsp, selector, gate_type, switched):
    """64-bit external interrupt: five qwords; preserve saved RF as supplied."""
    q30.require(gate_type in (14, 15) and selector & 3 == 0, "unsupported external interrupt gate")
    rsp = (selected_rsp & ~15) - 40
    q30.require(0 <= rsp <= q30.MASK - 39, "external interrupt stack overflow")
    flags = old["rflags"] & ~((1 << 8) | (1 << 16) | (1 << 14) | (1 << 17))
    if gate_type == 14:
        flags &= ~(1 << 9)
    return dict(rsp=rsp, slots=(old["rip"], old["cs"], old["rflags"] | (1 << 16 if old.get("partial_rep") else 0), old["rsp"], old["ss"]),
                cs=selector, ss=0 if switched else old["ss"], rflags=flags)


def metadata_trace(records, operations, ncpus, prefix_records, initial_counts):
    """Adapt validated Q32 scope markers to the shared Q31 recipe decoder.

    Q32 END counters belong to APIC replay. The adapter supplies no invented
    Q31 counter evidence; those fields are ignored only in interrupt mode.
    Every original Q31 state, recipe and native operation remains unchanged.
    """
    projected = []
    for record in records:
        sequence, kind, cpu, epoch, data = record
        if sequence > prefix_records and kind == 14 and data[0] == CASE:
            group, variant, rep, start, serial, target, role = data[1:]
            kind, data = 13, (q31.CASE, group, variant, rep, start, serial, ncpus, 0)
        elif sequence > prefix_records and kind == 14 and data[0] == END:
            serial, group, variant, rep, guest, native, boundaries = data[1:]
            kind, data = 13, (q31.END, serial, group, variant, rep, q30.MASK, q30.MASK, 0)
        if kind == 13:
            projected.append(record if kind == record[1] and data is record[4] else (sequence, kind, cpu, epoch, data))
    return q31.index_trace(projected, operations, ncpus, prefix_records, VARIANTS, initial_counts, interrupt_mode=True)


def guest_semantics(cases, apic_cases, ncpus, additional_instructions=(), nonretiring_probes=(), halt_trials=False, fixture_bases=None, scalar_fetches=False):
    for key, case in cases.items():
        scope = apic_cases[key]
        for cpu in range(ncpus):
            states, steps = case["states"][cpu], case["steps"][cpu]
            base = 0x10000000 + cpu * 0x20000 if fixture_bases is None else fixture_bases[key, cpu]
            interrupts, stops, boundaries = {}, {}, set()
            for sequence, subtype, data in scope["events"][cpu]:
                if subtype == BOUNDARY:
                    boundary = next((s for s in states if s["sequence"] > sequence), None)
                    q30.require(boundary is not None and boundary["point"] & 255 == 6
                                and (boundary["arch"]["rip"], boundary["arch"]["rflags"]) == data[1:3],
                                "guest boundary lacks exact retained canonical architecture")
                    previous = [s for s in states if s["end_sequence"] < sequence]
                    if previous:
                        q30.require(boundary["arch"] == previous[-1]["arch"],
                                    "guest boundary changed canonical committed/returned architecture")
                    boundaries.add(boundary["end_sequence"])
                    attempts = [a for a in case["attempts"][cpu] if a["sequence"] < sequence]
                    if attempts:
                        current = attempts[-1]
                        completed = [s for s in states if s["attempt"] == current["identity"] and s["sequence"] < sequence
                                     and s["point"] & 255 in (2, 5)]
                        q30.require(completed or current["instruction"] in (4, *nonretiring_probes),
                                    "guest interrupt boundary inside an uncommitted scalar instruction")
                    if data[5]:
                        entry = next((s for s in states if s["sequence"] > boundary["end_sequence"]), None)
                        q30.require(entry is not None and entry["point"] & 255 == 2
                                    and entry["arch"] == boundary["arch"], "IRQ exposed interior/partial instruction state")
                        partial = bool(attempts and current["instruction"] == 4 and not completed)
                        if partial:
                            before = next(s for s in states if s["attempt"] == current["identity"] and s["point"] & 255 == 1)
                            q30.require(0 < boundary["arch"]["rcx"] < before["arch"]["rcx"]
                                        and boundary["arch"]["rip"] == before["arch"]["rip"],
                                        "partial REP interrupt lacks actual completed prefix")
                        progress_marks = [d for s, k, d in scope["events"][cpu] if sequence < s < entry["sequence"] and k == CONTROL and d[0] == 12]
                        expected_mark = (12, 1, boundary["arch"]["rip"], boundary["arch"]["rcx"],
                                         boundary["arch"]["rsi"], boundary["arch"]["rdi"], data[0])
                        q30.require(progress_marks == ([expected_mark] if partial else []),
                                    "partial REP frame mode is not tied to actual completed progress")
                        interrupts[entry["end_sequence"]] = (data[5], partial)
                elif subtype == DISPATCH:
                    delivered = [d for seq, kind, d in case["events"][cpu] if kind == q31.DELIVER and seq < sequence][-1:]
                    q30.require(delivered and (delivered[0][1] < 32) == bool(data[4])
                                and (delivered[0][5], delivered[0][6]) == (data[2], data[3])
                                and (data[4] or delivered[0][1] == data[1]), "IRQ dispatch was not actual guest frame delivery")
                    frame_steps = [s for s in steps if s["sequence"] < sequence and s["action"] == 4]
                    q30.require(frame_steps and frame_steps[-1]["recipe"]["generation"] == data[6],
                                "IRQ frame lost recovery metadata generation")
                elif subtype == SHADOW:
                    attempts = [a for a in case["attempts"][cpu] if a["sequence"] < sequence and a["instruction"] == data[0]]
                    q30.require(attempts, "shadow transition has no actual instruction attempt")
                    attempt = attempts[-1]
                    own = [s for s in states if s["attempt"] == attempt["identity"] and s["sequence"] < sequence]
                    before = next(s for s in own if s["point"] & 255 == 1)
                    point = 3 if data[6] else 5
                    after = [s for s in own if s["point"] & 255 == point]
                    q30.require(after and int(bool(before["arch"]["rflags"] & 512)) == data[1]
                                and int(bool(after[-1]["arch"]["rflags"] & 512)) == data[2],
                                "shadow/IF metadata contradicts actual instruction or exception transfer")
                elif subtype == CONTROL and data[0] == 3:
                    stops[sequence] = data
            for attempt in case["attempts"][cpu]:
                if attempt["instruction"] in (8, 9, 11):
                    own = [s for s in states if s["attempt"] == attempt["identity"]]
                    before = next(s for s in own if s["point"] & 255 == 1)
                    if before["arch"]["cs"] & 3:
                        faults = [s for s in own if s["point"] & 255 == 2]
                        q30.require(len(faults) == 1 and faults[0]["arch"] == before["arch"], "privileged instruction fault changed architectural state")
                        interrupts[faults[0]["end_sequence"]] = (13, False)
            try:
                q31.delivery_semantics(case, cpu, interrupts, interrupt_frame, stops, base)
            except ValueError as error:
                raise ValueError(f"Q32 case {key} CPU {cpu}: {error}") from error
            ordinary = dict(case, attempts=[list(row) for row in case["attempts"]])
            ordinary["attempts"][cpu] = [a for a in case["attempts"][cpu] if a["instruction"] <= 7 and a["instruction"] not in additional_instructions]
            q31.attempt_semantics(ordinary, cpu, key[0], interrupts=interrupts, continuity=False, boundary_states=boundaries, scalar_fetches=scalar_fetches)
            for attempt in case["attempts"][cpu]:
                instruction = attempt["instruction"]
                if instruction <= 7 or instruction in additional_instructions:
                    continue
                own = [s for s in states if s["attempt"] == attempt["identity"]]
                before = [s for s in own if s["point"] & 255 == 1]
                committed = [s for s in own if s["point"] & 255 == 5]
                q30.require(len(before) == 1 and before[0]["arch"]["rip"] == attempt["rip"], "Q32 instruction lacks before-state")
                old = before[0]["arch"]
                expected = dict(old)
                q30.require((attempt["length"], attempt["width"]) ==
                            ({8: (1, 1), 9: (1, 1), 10: (2, 1), 11: (1, 1), 12: (1, 1), 14: (3, 8), 15: (3, 8), 16: (2, 1)}.get(instruction)),
                            "unsupported Q32 instruction encoding/width")
                if instruction in (8, 9, 11) and old["cs"] & 3:
                    faults = [s for s in own if s["point"] & 255 == 2]
                    delivered = [d for _, k, d in case["events"][cpu] if k == q31.DELIVER and d[0] == attempt["identity"]]
                    q30.require(not committed and faults and faults[0]["arch"] == old
                                and delivered and delivered[0][1:3] == (13, 0) and old["rflags"] & (3 << 12) == 0,
                                "CPL3 CLI/STI/HLT changed IF instead of exact #GP")
                    continue
                if instruction == 8:
                    expected["rflags"] &= ~512
                elif instruction == 9:
                    expected["rflags"] |= 512
                elif instruction == 10:
                    reads = [s["operation"] for s in steps if s["recipe"]["attempt"] == attempt["identity"] and s["action"] == 3]
                    q30.require(reads and reads[0]["begin"][:3] == (base + 0x10010, 0, 8)
                                and reads[0]["end"][1:3] == (0x00CF93000000FFFF, 0) and old["rax"] == 16,
                                "MOV SS omitted real descriptor validation")
                    expected["ss"] = 16
                elif instruction == 14:
                    stores = [s["operation"] for s in steps if s["recipe"]["attempt"] == attempt["identity"] and s["action"] == 2]
                    q30.require(len(stores) == 1 and stores[0]["begin"][:4] == (base + 0xC0F0, 1, 8, 0)
                                and stores[0]["end"][2] == 0 and (old["rdi"], old["rax"]) == (stores[0]["begin"][0], 0),
                                "guest EOI lacked its actual modeled MMIO store")
                elif instruction == 15:
                    expected["rsp"] = old["rax"]
                if instruction == 16:
                    fetches = [s["operation"] for s in steps if s["recipe"]["attempt"] == attempt["identity"] and s["action"] == 7]
                    q30.require(len(fetches) == 2 and [p["begin"][:3] for p in fetches] ==
                                [(old["rip"], 4, 1), (old["rip"] + 1, 4, 1)]
                                and all(p["end"][2] == 0 for p in fetches) and fetches[0]["end"][1] == 0xEB,
                                "handler JMP omitted actual opcode/displacement fetch")
                    offset = fetches[1]["end"][1]
                    expected["rip"] += offset if offset < 128 else offset - 256
                    retry = halt_trials and key[:2] == (7, 2) and old["rip"] == base + 0x3501
                    q30.require(expected["rip"] + 2 == base + (0x3500 if retry else 0x3460)
                                and (not retry or offset == 0xFD), "guest JMP targeted wrong handler/HLT site")
                expected["rip"] += attempt["length"]
                expected["rflags"] &= ~(1 << 16)
                q30.require(len(committed) == 1 and committed[0]["arch"] == expected, "Q32 instruction retired wrong register/flag state")
    return len(cases)


def halt_trial_semantics(case, scope, cpu, symbols, diagnostic_exhausted=False):
    """All bounded HLT episodes count; only the first real post-WFE arrival ends them."""
    events, states = scope["events"][cpu], case["states"][cpu]
    marks = [(s, d) for s, k, d in events if k == CONTROL and d[0] == 15]
    count = len(marks)
    q30.require(1 <= count <= 8 and [d[1] for s, d in marks] == list(range(1, count + 1)),
                "HLT trial ordinals missing, duplicated or beyond eight")
    q30.require(symbols is not None and (not diagnostic_exhausted or count == 8)
                and [d[4] for s, d in marks] == ([0] * 8 if diagnostic_exhausted else [0] * (count - 1) + [1]),
                "HLT trials omitted success or continued after first success")
    arrivals = [(s, d) for s, k, d in events if k == NATIVE]
    halts = [(s, d) for s, k, d in events if k == HALT]
    emits = [(s, d) for s, k, d in events if k == EMIT]
    dispatches = [(s, d) for s, k, d in events if k == DISPATCH]
    eois = [(s, d) for s, k, d in events if k == EOI]
    returns = [(s, d) for s, k, d in case["events"][cpu] if k == q31.RETURN]
    hlt = [a for a in case["attempts"][cpu] if a["instruction"] == 11]
    base = 0x10000000 + cpu * 0x20000
    retries = [a for a in case["attempts"][cpu] if a["instruction"] == 16 and a["rip"] == base + 0x3501]
    q30.require(len(arrivals) == len(emits) == len(dispatches) == len(eois) == len(returns) == len(hlt) == count
                and len(halts) == count * 3 and len(retries) == count - 1,
                "HLT trial omitted or discarded an actual arrival/HLT/EOI/IRET/retry")
    def state_for(attempt, point):
        found = [s for s in states if s["attempt"] == attempt["identity"] and s["point"] & 255 == point]
        q30.require(len(found) == 1, "HLT/retry lacks exact canonical state")
        return found[0]
    for index, ((mark_s, mark), (irq_s, irq)) in enumerate(zip(marks, arrivals)):
        q30.require(mark[2] == irq[2] and mark[3] == symbols["_q32_wait_after"]
                    and mark[4] == int(irq[2] == symbols["_q32_wait_after"])
                    and mark[5] == base + 0x3501 and mark[6] == irq[5]
                    and irq[0] == TIMER and irq[4] == 5 and irq[6] == cpu
                    and symbols["_q32_wait_begin"] <= irq[2] < symbols["_q32_wait_end"],
                    "HLT trial classification differs from actual native PC/source/ack")
        pub, recheck, wake = halts[index * 3:index * 3 + 3]
        emitted, dispatch, eoi, returned = emits[index], dispatches[index], eois[index], returns[index]
        before, committed = state_for(hlt[index], 1), state_for(hlt[index], 5)
        restored = [s for s in states if s["sequence"] > returned[0] and s["point"] & 255 == 4]
        q30.require(restored, "HLT trial lacks real IRET restored state")
        restored = restored[0]
        q30.require(hlt[index]["rip"] == base + 0x3500 and before["arch"]["rflags"] & 512
                    and [pub[1][0], recheck[1][0], wake[1][0]] == [1, 2, 3]
                    and all(d[4] == mark[5] for _, d in (pub, recheck, wake))
                    and committed["end_sequence"] < pub[0] < recheck[0] < emitted[0] < irq_s < mark_s < wake[0]
                    < dispatch[0] < eoi[0] < returned[0] < restored["sequence"]
                    and emitted[1][:3] == (TIMER, cpu, cpu) and emitted[1][4] == 0x41
                    and dispatch[1][1] == eoi[1][0] == returned[1][1] == 0x41 and dispatch[1][4] == 0
                    and restored["arch"] == committed["arch"],
                    "HLT trial rearmed or stopped without complete real interrupt service")
        if index + 1 < count:
            retry = retries[index]
            rbefore, rcommit = state_for(retry, 1), state_for(retry, 5)
            following = state_for(hlt[index + 1], 1)
            fetches = [s["operation"] for s in case["steps"][cpu]
                       if s["recipe"]["attempt"] == retry["identity"] and s["action"] == 7]
            expected = dict(restored["arch"], rip=base + 0x3500, rflags=restored["arch"]["rflags"] & ~(1 << 16))
            q30.require(restored["end_sequence"] < retry["sequence"] < rcommit["end_sequence"] < hlt[index + 1]["sequence"]
                        and rbefore["arch"] == restored["arch"] and rcommit["arch"] == following["arch"] == expected
                        and len(fetches) == 2 and all(p["begin"][:3] == (base + 0x3501 + i, 4, 1)
                        and p["end"][2] == 0 and p["end"][1] == (0xEB, 0xFD)[i] for i, p in enumerate(fetches)),
                        "HLT retry skipped encoded EB FD or changed restored architectural context")
    return count


def finite_semantics(cases, apic_cases, ncpus, halt_trials=False, symbols=None):
    applicable = 0
    for (group, variant, rep), case in cases.items():
        scope = apic_cases[(group, variant, rep)]
        for cpu in range(ncpus):
            events, states, steps = scope["events"][cpu], case["states"][cpu], case["steps"][cpu]
            base = 0x10000000 + cpu * 0x20000
            controls = [d for _, k, d in events if k == CONTROL]
            excluded = ncpus == 2 and (group == 6 and variant == 0 or group == 11)
            q30.require(sum(d[0] == 5 for d in controls) == int(excluded), "finite applicability was omitted or invented")
            dispatch = [(s, d) for s, k, d in events if k == DISPATCH]
            native = [(s, d) for s, k, d in events if k == NATIVE]
            delivered = [(s, d) for s, k, d in case["events"][cpu] if k == q31.DELIVER]
            vectors = [d[1] for _, d in delivered]
            trial_count = halt_trial_semantics(case, scope, cpu, symbols) if halt_trials and (group, variant) == (7, 2) else 1
            expected = ([0x41] * trial_count if halt_trials and (group, variant) == (7, 2) else [] if excluded or group == 10 and variant == 2 else
                        [13] if group == 3 and variant in (1, 2) else
                        [14, 0x41] if (group, variant) in ((4, 5), (8, 2), (9, 1)) else
                        [0x71, 0x4F, 0x41] if (group, variant) == (5, 0) else
                        [0x41, 0x71, 0x4F] if (group, variant) == (5, 1) else
                        [0x71, 0x41] if (group, variant) == (5, 2) else
                        [] if (group, variant) == (7, 3) else
                        [0x41, 14] if (group, variant) in ((9, 0), (10, 0)) else
                        [14] if (group, variant) == (9, 2) else [0x41])
            q30.require(vectors == expected, f"Q32 {group}/{variant}/{rep} CPU {cpu}: actual guest vector/frame order")
            software = group in (4, 5) or group == 9 and variant in (0, 2)
            no_native = excluded or software or group == 3 and variant in (1, 2) or group == 10 and variant == 2
            q30.require(len(native) == (0 if no_native else trial_count), f"Q32 {group}/{variant}/{rep} CPU {cpu}: native count {len(native)} expected {0 if no_native else 1}")
            if native:
                expected_source = variant + 1 if group in (1, 2) else HOST if (group, variant) == (6, 1) else PEER if group in (6, 11) or group == 12 and variant else TIMER
                q30.require(native[0][1][0] == expected_source, "finite case exercised wrong native source")
            q30.require(len(dispatch) == len([v for v in expected if v >= 32]) + int((group, variant) == (9, 2)),
                        "finite case invented or omitted guest IRQ dispatch")
            handler_stores = [s for s in steps if s["action"] == 2 and s["operation"]["begin"][:3] == (base + 0xC100, 1, 8)
                              and not s["operation"]["end"][2]]
            q30.require(len(handler_stores) == sum(not d[4] for _, d in dispatch),
                        "IRQ handler duplicated or omitted an actual RAM increment")
            # Every ordinary interrupt performs one coherent handler memory increment,
            # then restores the saved operand registers through actual guest loads.
            for seq, data in dispatch:
                if data[4]:
                    continue
                boundary_seq = max(s for s, k, d in events if k == BOUNDARY and s < seq and d[0] == data[0])
                entry = next(s["arch"] for s in states if s["sequence"] > boundary_seq)
                returned = next(((s, d) for s, k, d in case["events"][cpu]
                                 if k == q31.RETURN and s > seq and d[1] == data[1] and d[2] == data[2]), None)
                q30.require(returned is not None, "ordinary external interrupt omitted IRET")
                stop = returned[0]
                slot = base + 0xC200 + data[3] * 32
                own = [s for s in steps if seq < s["sequence"] < stop]
                for address, value in ((slot, entry["rax"]), (slot + 8, entry["rdi"])):
                    writes = [s["operation"] for s in own if s["action"] == 6 and s["operation"]["begin"][:3] == (address, 1, 8)]
                    reads = [s["operation"] for s in own if s["action"] == 6 and s["operation"]["begin"][:3] == (address, 0, 8)]
                    q30.require(len(writes) == len(reads) == 1 and writes[0]["begin"][3] == reads[0]["end"][1] == value
                                and not writes[0]["end"][2] and not reads[0]["end"][2], "IRQ handler lost saved guest operand registers")
                effects = [s["operation"] for s in own if s["action"] == 2 and s["operation"]["begin"][:3] == (base + 0xC100, 1, 8)]
                # Nested entries have their own depth; select the first effect of this entry.
                q30.require(effects, "guest IRQ handler had no coherent RAM effect")
                effect = effects[0]
                previous = [s["operation"] for s in own if s["action"] == 6 and s["sequence"] < effect["sequence"]
                            and s["operation"]["begin"][:3] == (base + 0xC100, 0, 8)]
                q30.require(previous and effect["begin"][3] == previous[-1]["end"][1] + 1 and effect["end"][2] == 0,
                            "guest IRQ handler effect is not one actual increment")
                restored = max((s for s in states if s["sequence"] < stop), key=lambda s: s["sequence"])
                q30.require((restored["arch"]["rax"], restored["arch"]["rdi"]) == (entry["rax"], entry["rdi"]),
                            "IRET canonical state omitted actual restored operand registers")
            if group in (3, 4, 7) and not (group == 3 and variant in (1, 2)):
                wanted = {(3, 0): [8, 9, 12], (4, 0): [9, 12], (4, 1): [9], (4, 2): [9, 8, 9, 12],
                          (4, 3): [9, 11], (4, 4): [10, 15], (4, 5): [9, 1, 1],
                          (7, 0): [11], (7, 1): [11], (7, 2): [11], (7, 3): [8, 11]}[(group, variant)]
                if halt_trials and (group, variant) == (7, 2): wanted = [11, 16] * (trial_count - 1) + [11]
                program = [a for a in case["attempts"][cpu] if base + 0x3500 <= a["rip"] < base + 0x3520]
                q30.require([a["instruction"] for a in program] == wanted, f"Q32 {group}/{variant}/{rep} CPU {cpu}: shadow instructions {[a['instruction'] for a in program]} wanted {wanted}")
                address = base + 0x3500
                for attempt in program:
                    q30.require(attempt["rip"] == address, "interrupt shadow crossed an unrecorded guest control transfer")
                    if any(s["attempt"] == attempt["identity"] and s["point"] & 255 == 5 for s in states):
                        address = base + 0x3500 if halt_trials and (group, variant) == (7, 2) and attempt["instruction"] == 16 else address + attempt["length"]
            if group == 6 and variant == 0 and not excluded:
                accepts = [(s, d) for s, k, d in events if k == ACCEPT]
                collapse = [(s, d) for s, k, d in events if k == CONTROL and d[0] == 6]
                q30.require(len(collapse) == 1 and len(accepts) == 2 and collapse[0][0] < native[0][0]
                            and accepts[0][1][6] == accepts[1][1][6] and [d[5] for _, d in accepts] == [0, 1],
                            "two peer payloads did not coalesce into one pending vector under one native notification")
            if group == 11 and not excluded:
                leader = rep % 4
                sends = [d for _, k, d in scope["events"][leader] if k == CONTROL and d[0] == 13]
                q30.require(len(sends) == 4 and {d[2] for d in sends} == set(range(4)), "broadcast omitted actual send completion for a target")
                emits = [d for row in scope["events"] for _, k, d in row if k == EMIT]
                q30.require(len(emits) == 4 and {d[2] for d in emits} == set(range(4))
                            and all(d[0] == PEER and d[1] == leader and d[4:] == (0x41, variant, 1) for d in emits),
                            "broadcast native payloads do not exactly match leader and all recipients")
                role = (cpu + 4 - leader) % 4
                boundaries = [d for _, k, d in events if k == BOUNDARY]
                if role == 1:
                    q30.require(boundaries and boundaries[0][2] & 512 == 0 and boundaries[0][5] == 0,
                                "broadcast masked target did not retain pending interrupt")
                    release = [a for a in case["attempts"][cpu] if base + 0x3580 <= a["rip"] < base + 0x3590]
                    q30.require([(a["instruction"], a["rip"]) for a in release] == [(9, base + 0x3580), (12, base + 0x3581)],
                                "broadcast masked target omitted contiguous STI/NOP release")
                elif role == 2:
                    q30.require(boundaries and boundaries[0][6] == 1 and boundaries[0][5] == 0x41,
                                "broadcast HLT target did not perform an eligible guest wake")
            if group == 10:
                rep_attempts = [a for a in case["attempts"][cpu] if a["instruction"] == 4]
                before = [next(s["arch"] for s in states if s["attempt"] == a["identity"] and s["point"] & 255 == 1)
                          for a in rep_attempts]
                q30.require([s["rcx"] for s in before] == ([8, 4, 4] if variant == 0 else [8, 4] if variant == 1 else [0]),
                            "REP IRQ/fault restart lost actual proper-prefix count")
                stores = [s["operation"] for s in steps if s["action"] == 9 and not s["operation"]["end"][2]]
                addresses = list(range(base + 0xDFFC, base + 0xE004))
                if variant == 1:
                    addresses.reverse()
                q30.require([p["begin"][0] for p in stores] == ([] if variant == 2 else addresses),
                            "REP duplicated or skipped a committed destination byte")
                observations = [s["operation"] for s in steps if s["action"] == 6 and s["operation"]["begin"][1:3] == (0, 1)
                                and base + 0xDFFC <= s["operation"]["begin"][0] < base + 0xE004]
                q30.require(len(observations) == 8 and [p["end"][1] for p in observations] ==
                            ([0] * 8 if variant == 2 else list(range(0xA0, 0xA8))), "REP final byte observations disagree")
            applicable += not excluded
    return dict(global_cases=len(cases), applicable_cpu_cases=applicable, not_applicable_cpu_cases=len(cases) * ncpus - applicable,
                not_applicable=([dict(group=group, variant=variant, repetitions=16, cpu_cases=32, minimum_vcpus=minimum)
                                 for group, variant, minimum in ((6, 0, 3), (11, 0, 4), (11, 1, 4))] if ncpus == 2 else []))


def native_semantics(cases, apic_cases, records, operations, symbols, ncpus, halt_trials=False):
    gates, phase, owner, faults = {}, 0, None, {}
    for sequence, kind, cpu, epoch, data in records:
        if kind == q30.FAULT:
            faults.setdefault(data[0], []).append(sequence)
        if kind == q30.GATE:
            phase, owner = data[0], cpu if data[0] else None
        if kind == 14 and data[0] == NATIVE:
            gates[sequence] = phase, owner
    observed = set()
    for (group, variant, rep), case in cases.items():
        scope = apic_cases[(group, variant, rep)]
        for cpu in range(ncpus):
            events = scope["events"][cpu]
            arrivals = [(s, d) for s, k, d in events if k == NATIVE]
            if halt_trials and (group, variant) == (7, 2):
                halt_trial_semantics(case, scope, cpu, symbols)
            for sequence, data in arrivals:
                source, intid, elr, spsr, stage, ack, target = data
                role = (cpu + 4 - rep % 4) % 4
                wait = group == 7 and variant in (2, 3) or group == 11 and role == 2
                held = group != 6 or variant == 1
                if group == 11:
                    held = role in (1, 2, 3)
                if held:
                    label = "wait" if wait else "hold"
                    q30.require(symbols[f"_q32_{label}_begin"] <= elr < symbols[f"_q32_{label}_end"],
                                "native notification arrived outside required interior helper interval")
                if group == 7 and variant == 2 and not halt_trials:
                    q30.require(elr == symbols["_q32_wait_after"], "HLT wake has no actual post-WFE native PC witness")
                expected_stage = (variant + 2 if group == 8 else 5 if wait else
                                  3 if group == 11 and role == 3 else 6 if group == 12 and variant == 0 else
                                  0 if group == 11 and not held else 1)
                q30.require(stage == expected_stage, "native IRQ did not arrive in declared instruction/shim/halt stage")
                if group == 2:
                    attempts = [a for a in case["attempts"][cpu] if a["instruction"] == 3 and a["sequence"] < sequence]
                    q30.require(len(attempts) == 1, "interior IRQ has no active real XADD instruction")
                    identity = attempts[0]["identity"]
                    reads = [s for s in case["steps"][cpu] if s["recipe"]["attempt"] == identity and s["action"] == 1]
                    writes = [s for s in case["steps"][cpu] if s["recipe"]["attempt"] == identity and s["action"] == 2]
                    q30.require(len(reads) == len(writes) == 1 and reads[0]["sequence"] < sequence < writes[0]["operation"]["sequence"],
                                "IRQ did not expose XADD interior between operand read and result store")
                if group == 8 and variant == 0:
                    q30.require(any(p["cpu"] == cpu and p["sequence"] < sequence < p["end_sequence"]
                                    and any(s < sequence for s in faults.get(p["sequence"], [])) for p in operations),
                                "repair IRQ was not inside a real native memory-fault recovery")
                if group == 8 and variant == 1 or group == 11 and role == 3:
                    q30.require(gates[sequence] == (1, cpu), "tracking IRQ lacks actual exclusive paging epoch")
                if group == 12 and variant == 0:
                    q30.require(gates[sequence] == (1, cpu) and any(p["cpu"] == cpu and p["sequence"] < sequence < p["end_sequence"]
                                and p["begin"][:4] == (0x1000C020 + cpu * 0x20000, 1, 8, 0xC30000002BB8)
                                and p["faults"] > 0 and p["mutations"] for p in operations),
                                "local code-write IRQ escaped actual trapped store and exclusive epoch")
                if group == 8 and variant == 2:
                    fault_states = [s for s in case["states"][cpu] if s["point"] & 255 == 2 and s["end_sequence"] < sequence]
                    deliveries = [s for s, k, d in case["events"][cpu] if k == q31.DELIVER and d[1] == 14 and s > sequence]
                    q30.require(fault_states and deliveries, "exception-work IRQ lacks actual guest fault/frame interval")
                if group == 7:
                    halt = [(s, d) for s, k, d in events if k == HALT]
                    q30.require(halt and halt[0][1][0] == 1, "HLT omitted publication")
                    rechecks = [s for s, d in halt if d[0] == 2]
                    if variant == 0:
                        q30.require(sequence < halt[0][0], "pre-publication wake arrived late")
                    elif variant == 1:
                        q30.require(rechecks and halt[0][0] < sequence < rechecks[0], "publication/recheck race not observed")
                    else:
                        q30.require(rechecks and rechecks[0] < sequence and any(s > sequence and d[0] == 3 for s, d in halt),
                                    "halt wake omitted actual sleep/recheck/native-return order")
                observed.add((group, variant, rep, cpu))
    return len(observed)


def code_semantics(cases, apic_cases, records, ncpus, native_protocol=True):
    code_records = [r for r in records if r[1] == 13 and r[4][0] == q31.CODE]
    marks = [r for r in records if r[1] == q30.MARK and r[4][0] == 1 and r[4][2] == 0x33]
    gate_epochs = {}
    for s, k, c, e, d in records:
        if k == q30.GATE:
            gate_epochs.setdefault(e, []).append((s, d[0]))
    for (group, variant, rep), case in cases.items():
        if group != 12:
            continue
        scope = apic_cases[(group, variant, rep)]
        for cpu in range(ncpus):
            attempts, states = case["attempts"][cpu], case["states"][cpu]
            dispatches = [a for a in attempts if a["instruction"] == 5]
            q30.require(len(dispatches) == 2, "code interrupt case omitted old/fresh native byte dispatch")
            results = [next(s["arch"]["rax"] for s in states if s["attempt"] == a["identity"] and s["point"] & 255 == 5)
                       for a in dispatches]
            q30.require(results == [42, 43], "post-write code dispatch executed stale or mixed instruction bytes")
            cpuid = [a for a in attempts if a["instruction"] == 7]
            q30.require(len(cpuid) == 1 and dispatches[0]["sequence"] < cpuid[0]["sequence"] < dispatches[1]["sequence"],
                        "fresh dispatch omitted named guest CPUID serialization")
            events = case["events"][cpu]
            pins = [(s, d) for s, k, d in events if k == q31.CODE and d[0] == 3]
            unpins = [(s, d) for s, k, d in events if k == q31.CODE and d[0] == 4]
            irq = next((s, d) for s, k, d in scope["events"][cpu] if k == DISPATCH)
            held = [(s, d) for s, d in pins if s < irq[0] and not any(s < end < irq[0] and out[1] == d[1] for end, out in unpins)]
            q30.require(len(held) == 1 and irq[1][6] == held[0][1][5], "IRQ frame did not retain old pinned recovery generation")
            page, old = held[0][1][1], held[0][1][5]
            writes = [(s, d) for s, k, c, e, d in code_records if scope["start"] < s < irq[0]
                      and k == 13 and d[0] == q31.CODE and d[1] == 2 and d[2] == page and d[3] == old and d[4] == old + 1 and d[5]]
            q30.require(len(writes) == 1, "IRQ old pin did not span an actual tracked code mutation")
            retired = [(s, d) for s, d in unpins if irq[0] < s < cpuid[0]["sequence"] and d[1] == page]
            latest = [d[4] for s, k, c, e, d in code_records if scope["start"] < s < dispatches[1]["sequence"]
                      and d[1] == 2 and d[2] == page]
            q30.require(len(retired) == 1 and latest and latest[-1] > old
                        and dispatches[1]["generation"] == latest[-1], "old code metadata retired early or fresh dispatch reused old generation")
            commits = [s for s in states if s["attempt"] == cpuid[0]["identity"] and s["point"] & 255 == 5]
            if not native_protocol:
                continue
            invalidations = [(s, e) for s, k, c, e, d in marks if c == cpu and k == q30.MARK
                             and d[:3] == (1, 0x10000000 + cpu * 0x20000 + (0xC020 if variant == 0 else 0x1B020), 0x33) and cpuid[0]["sequence"] < s < commits[0]["sequence"]]
            q30.require(len(invalidations) == 1, "guest CPUID omitted actual paging serialization operation")
            epoch = invalidations[0][1]
            gates = gate_epochs.get(epoch, [])
            q30.require([d for _, d in gates] == [1, 2, 0] and cpuid[0]["sequence"] < gates[0][0] < invalidations[0][0]
                        < gates[1][0] < gates[2][0] < commits[0]["sequence"], "CPUID retired before actual gate/ACK completion")


def diagnostic_semantics(logs, cpu, apic_cases):
    diagnostics = [r for r in logs if r.get("cpu") == cpu and "q32_diagnostic_stop" in r]
    requests = [(d, next(out for _, k, out in case["events"][cpu] if k == CONTROL and out[0] == 2))
                for case in sorted(apic_cases.values(), key=lambda c: c["serial"])
                for _, k, d in case["events"][cpu] if k == CONTROL and d[0] == 14]
    q30.require(len(diagnostics) == len(requests) == 16, "host masked-HLT observations incomplete")
    for host, (request, completion) in zip(diagnostics, requests):
        q30.require(host["q32_diagnostic_stop"] == "masked-halt"
                    and (host["sequence"], host["case"], host["flags"], host["rip"], host["irr_word1"], host["halted"])
                    == request[1:] and host["native_count"] == completion[4]
                    and completion[5] == completion[6] == request[1],
                    "host masked-HLT observation did not match actual published request and acknowledgement")


def check(artifact, logs, sym, ram, initial, version=4, terminal=True, snapshot_only=False):
    q30.require(version in (4, 5, 6) and (not snapshot_only or version >= 5 and not terminal), "explicit Q32 checkpoint mode")
    sym = dict(sym, _text_end=min(sym[k] for k in ("_l1", "_l2", "_device_l2", "_pages", "_aliases")))
    saved = q30.snapshot(artifact("q31-checkpoint.bin"), version)
    previous = q31.check(artifact, logs, sym, saved, initial, version=version, terminal=False, snapshot_only=True)
    q30.require(previous["status"] == "PASS", "Q31 checkpoint did not retain complete independent acceptance")
    prefix, prefix_cpus, _, unused = q30.decode(saved, initial, version=version, records_required=False)
    del unused
    saved_q31, saved_cpus, _, _, _ = q31.decode(saved, prefix["ncpus"], version=version)
    saved_q32, _ = decode(saved, prefix["ncpus"])
    header, paging_cpus, table_roles, records = q30.decode(ram, initial, version=version)
    ncpus, prefix_records = header["ncpus"], prefix["records"]
    q30.require(ncpus == prefix["ncpus"] and header["records"] > prefix_records, "Q32 reset or failed to continue Q31")
    end = q30.LOG_OFFSET + prefix_records * 96
    q30.require(memoryview(saved)[q30.LOG_OFFSET:end] == memoryview(ram)[q30.LOG_OFFSET:end], "Q31 retained log prefix changed")
    qheader, cpus = decode(ram, ncpus, required_version=2 if version >= 5 else 1)
    halt_trials = qheader["version"] == 2
    routes = [r for r in logs if "q32_spi_base" in r]
    q30.require(len(routes) == 1 and tuple(routes[0][key] for key in
                ("q32_spi_base", "q32_spi_count", "q32_sgi", "q32_eoimode", "q32_native_group")) ==
                (qheader["spi_base"], qheader["spi_count"], 2, 0, 1), "host/native fixed interrupt route configuration mismatch")
    q30.require(qheader["cases"] == 592 and qheader["phase"] == 2
                and (qheader["checkpoint_records"], qheader["checkpoint_epoch"]) == (prefix_records, prefix["epoch"])
                and saved_q32 == dict(qheader, cases=0, phase=0)
                and not any(saved[STATE + 96:STATE + STATE_BYTES]), "Q32 checkpoint identity or premature execution")
    q30.require(snapshot_only or artifact("q32.bin").read_bytes() == ram[STATE:STATE + STATE_BYTES]
                and artifact("q31.bin").read_bytes() == ram[q31.STATE:q31.STATE + q31.STATE_BYTES], "separate continuation dumps disagree")
    summary = [r for r in logs if "q31_checkpoint_dump_complete" in r]
    q30.require(len(summary) == 1 and summary[0]["q31_checkpoint_dump_complete"] is True
                and summary[0]["q31_checkpoint_host_checks_pass"] is True
                and (summary[0]["q31_checkpoint_ram_bytes"], summary[0]["q31_checkpoint_records"], summary[0]["q31_checkpoint_epoch"])
                == (len(saved), prefix_records, prefix["epoch"]), "same-VM all-thread Q31 checkpoint missing")
    for cpu in range(ncpus):
        stops = [r for r in logs if r.get("cpu") == cpu and "q31_checkpoint_instruction_pc" in r]
        q30.require(len(stops) == 1, "missing Q31 checkpoint owning-thread registers")
        stop = stops[0]
        q30.require(stop["q31_checkpoint_register_check"] is True and stop["q31_checkpoint_actlr"] & 2
                    and not stop["q31_checkpoint_cpsr"] & 128
                    and stop["q31_checkpoint_resume_pc"] == stop["q31_checkpoint_instruction_pc"] + 4
                    and struct.unpack_from("<I", saved, stop["q31_checkpoint_instruction_pc"] - 0x40000000)[0] == 0xD4000682
                    and tuple(stop["q31_checkpoint_" + name] for name in ("tcr", "ttbr0", "ttbr1")) ==
                    tuple(prefix_cpus[cpu]["final_" + name] for name in ("tcr", "ttbr0", "ttbr1")), "Q31 checkpoint native register/translation mismatch")
    del saved
    code = CodeState()
    result = q30.replay(initial, records, ncpus, version=version, code_state=code)
    q30.foundation(ram, paging_cpus, sym, terminal=terminal, version=version)
    q30.validate_native_faults(records, sym)
    q30.validate_snapshot(ram, header, paging_cpus, table_roles, result)
    apic_cases, apic = apic_trace(records, ncpus, q30.word(ram, 0x800000 + 32), qheader["spi_base"], sym, prefix_records)
    cases, counts = metadata_trace(records, result["operations"], ncpus, prefix_records, saved_cpus)
    guest_semantics(cases, apic_cases, ncpus, halt_trials=halt_trials)
    applicability = finite_semantics(cases, apic_cases, ncpus, halt_trials=halt_trials, symbols=sym)
    native_cases = native_semantics(cases, apic_cases, records, result["operations"], sym, ncpus, halt_trials=halt_trials)
    code_semantics(cases, apic_cases, records, ncpus)
    final_q31, final_cpus, generations, users, roles = q31.decode(ram, ncpus, version=version)
    q30.require(final_q31 == saved_q31 and tuple(code.generations) == generations and tuple(code.users) == users
                and bytes(code.roles) == roles and not any(users) and not any(roles) and not code.pending_generation
                and not any(code.pins.values()), "continued Q31 identity or final code ownership retirement mismatch")
    for cpu, state in enumerate(cpus):
        replayed = apic[cpu]
        q30.require(state["started"] == state["done"] == 1 and state["cases"] == 592
                    and state["stage"] == state["shadow"] == state["tpr"] == state["halted"] == state["hook_stage"] == 0
                    and not bitmap(state["irr"]) and not bitmap(state["isr"])
                    and state["timer_end"] > state["timer_start"] >= saved_cpus[cpu]["timer_end"], "Q32 final CPU completion/idle/timer state")
        for name in ("native_count", "guest_count", "boundaries", "wake_generation"):
            q30.require(state[name] == replayed[name], "Q32 final replay counter: " + name)
        q30.require(state["handler_effects"] == state["guest_count"], "guest handler effect counter mismatch")
        config = replayed["config"]
        foundation = dict(zip(q30.FOUNDATION_FIELDS, paging_cpus[cpu]["foundation"][:22]))
        q30.require(config is not None and config[5] == foundation["redistributor"], "native GIC CPU-interface configuration missing")
        q30.require(state["diagnostic_request"] == state["diagnostic_ack"] == replayed["diagnostic_count"] == 16,
                    "masked HLT diagnostic was not acknowledged by external observer")
        diagnostic_semantics(logs, cpu, apic_cases)
        for source in range(3):
            for sender in range(4):
                key = source + 1, sender
                for name in ("emitted", "accepted", "vectors"):
                    q30.require(state[name][source][sender] == replayed[name].get(key, 0), "Q32 mailbox raw/replay disagreement")
                q30.require(state["emitted"][source][sender] == state["accepted"][source][sender], "unaccepted native payload at completion")
        q30.require(state["raw_irq"][1:] == tuple(replayed["raw_irq"][1:])
                    and state["empty_irq"][1:] == tuple(replayed["empty_irq"][1:])
                    and state["raw_irq"][0] == state["empty_irq"][0] + sum(d[0] == TIMER for _, d in replayed["native"])
                    and state["sgi_sent"] == tuple(replayed["sgi_sent"]), "unaccounted native source acknowledgements or SGI sends")
        q30.require(state["spi_request"] == state["spi_sent"] == state["spi_deassert"] == state["spi_deasserted"]
                    == state["emitted"][2][cpu] and state["spi_level"] == 0, "host SPI assertion/deassertion left pending")
        spi = [r for r in logs if "q32_spi_action" in r and r.get("cpu") == cpu
               and (not snapshot_only or r["sequence"] <= state["spi_request"])]
        q30.require([(r["q32_spi_action"], r["intid"], r["sequence"]) for r in spi] ==
                    [(action, qheader["spi_base"] + cpu, sequence) for sequence in range(1, state["spi_request"] + 1)
                     for action in ("assert", "deassert")], "host SPI transitions do not match all actual payloads")
        for name in ("attempts", "deliveries", "commits", "snapshots"):
            q30.require(final_cpus[cpu][name] == counts[cpu][name], "continued Q31 metadata counter mismatch")
        q30.require(final_cpus[cpu]["cases"] == saved_cpus[cpu]["cases"] and final_cpus[cpu]["depth"] == 0
                    and final_cpus[cpu]["code_writes"] == code.writes[cpu]
                    and final_cpus[cpu]["arch"] == cases[(12, 1, 15)]["states"][cpu][-1]["arch"], "final continued guest architecture/lifetime mismatch")
        final_case = cases[(12, 1, 15)]
        recipe, attempt = final_case["steps"][cpu][-1]["recipe"], final_case["attempts"][cpu][-1]
        metadata = dict(identity=recipe["rip"], generation=recipe["generation"], rip=recipe["rip"],
                        length=attempt["length"], instruction=recipe["instruction"], access=recipe["access"],
                        width=recipe["width"], substep=recipe["substep"])
        q30.require(final_cpus[cpu]["metadata"] == metadata and final_cpus[cpu]["current_attempt"] == counts[cpu]["attempts"]
                    and all(final_cpus[cpu][key] == saved_cpus[cpu][key] for key in ("started", "done", "cases", "timer_start", "timer_end")),
                    "continued Q31 metadata or preserved completion counters mismatch")
        hosts = [r for r in logs if r.get("cpu") == cpu and "q32_started" in r]
        q30.require(snapshot_only or len(hosts) == 1 and all(hosts[0]["q32_" + name] == state[name] for name in
                    ("started", "done", "cases", "native_count", "guest_count", "boundaries", "timer_start", "timer_end")), "Q32 host/raw CPU summary mismatch")
    return dict(format="natemu-q32-checkpoint-check-v1" if snapshot_only else "natemu-q32-independent-check-v1", status="INCOMPLETE" if result["incomplete"] else "PASS",
                checkpoint=previous, records=len(records), operations=len(result["operations"]), applicability=applicability,
                native_cpu_cases=native_cases, checked=["unchanged accepted Q30 and Q31 checkpoints in one VM",
                "full initial-to-final native paging and code-generation replay", "37 finite interrupt variants at 16 repetitions",
                "actual native source/interior arrival and guest handler/frame/IRET effects", "APIC priority, interrupt shadows and HLT races"],
                unsupported=sorted(result["incomplete"]))


def model_trace(path):
    with path.open() as stream:
        header = json.loads(next(stream))
        q30.require(header["format"] == "natemu-q32-model-trace-v1", "Q32 synthetic trace schema")
        records = [(s, k, c, e, tuple(d)) for s, k, c, e, d in map(json.loads, stream)]
    q30.require(header.get("q32_version") == 2, "Q32 synthetic trace lacks format2 HLT trial contract")
    halt_trials = True
    ncpus = header["ncpus"]
    contexts, pending, operations = [q30.DEFAULT_CONTEXT] * ncpus, {}, []
    for sequence, kind, cpu, epoch, data in records:
        if kind == q30.CONTEXT_EVENT:
            contexts[cpu] = data[:5]
        elif kind == q30.BEGIN:
            pending[cpu] = dict(sequence=sequence, cpu=cpu, begin=data, context=contexts[cpu])
        elif kind == q30.END:
            operation = pending.pop(cpu)
            q30.require(data[0] == operation["sequence"], "model native operation linkage")
            operation.update(end_sequence=sequence, end=data, end_walk={"gpa": data[4]})
            operations.append(operation)
    q30.require(not pending, "unfinished model operations")
    apic_cases, cpus = apic_trace(records, ncpus, header["timer_intid"], header["spi_base"], header["symbols"], 0)
    cases, counts = metadata_trace(records, operations, ncpus, 0, header["initial_q31_counts"])
    guest_semantics(cases, apic_cases, ncpus, halt_trials=halt_trials)
    header["checked_applicability"] = finite_semantics(cases, apic_cases, ncpus, halt_trials=halt_trials, symbols=header["symbols"])
    code_semantics(cases, apic_cases, records, ncpus, native_protocol=False)
    return header, records, operations, cases, apic_cases, counts, cpus


def model_controls(cases, apic_cases, ncpus, records, operations, header):
    rejected = []
    def corrupt(name, key, change, validator=guest_semantics):
        selected, apic = {key: copy.deepcopy(cases[key])}, {key: copy.deepcopy(apic_cases[key])}
        change(selected[key], apic[key])
        try:
            validator(selected, apic, ncpus)
        except (ValueError, StopIteration):
            rejected.append(name)
        else:
            raise AssertionError("Q32 real-pattern corruption accepted: " + name)
    def frame_flag(case, scope):
        first = next(s for s in case["steps"][0] if s["action"] == 4 and s["operation"]["begin"][0] & 4095 == 0x5FE0 & 4095)
        data = list(first["operation"]["begin"]); data[3] ^= 1 << 16; first["operation"]["begin"] = tuple(data)
    def change_frame(case, scope):
        pushes = [s for s in case["steps"][0] if s["action"] == 4]
        data = list(pushes[2]["operation"]["begin"]); data[3] ^= 1 << 16; pushes[2]["operation"]["begin"] = tuple(data)
    for variant in (0, 1):
        corrupt("partial REP saved RF cleared direction " + str(variant), (10, variant, 0), change_frame)
    corrupt("ordinary IRQ saved RF forced", (1, 0, 0), change_frame)
    def gpr(case, scope):
        next(s for s in case["states"][0] if s["point"] & 255 == 3)["arch"]["rax"] ^= 1
    corrupt("interior operand leaked into IRQ frame state", (2, 0, 0), gpr)
    def coherent_boundary(case, scope):
        start = next(s for s, k, d in scope["events"][0] if k == BOUNDARY)
        for state in case["states"][0]:
            if state["sequence"] > start:
                state["arch"]["rdx"] ^= 1
    corrupt("coherent wrong GPR across boundary/frame/IRET", (2, 0, 0), coherent_boundary)
    def iret(case, scope):
        case["steps"][0].pop(next(i for i, s in enumerate(case["steps"][0]) if s["action"] == 5))
    corrupt("IRET bypassed coherent frame load", (1, 0, 0), iret)
    def effects(case, scope):
        step = next(s for s in case["steps"][0] if s["action"] == 2 and s["operation"]["begin"][0] == 0x1000C100)
        data = list(step["operation"]["begin"]); data[3] += 1; step["operation"]["begin"] = tuple(data)
    corrupt("handler duplicated RAM increment", (1, 0, 0), effects, finite_semantics)
    def duplicate_effect(case, scope):
        step = next(s for s in case["steps"][0] if s["action"] == 2 and s["operation"]["begin"][0] == 0x1000C100)
        duplicate = copy.deepcopy(step)
        duplicate["sequence"] += 1
        duplicate["operation"]["sequence"] += 1
        case["steps"][0].append(duplicate)
    corrupt("extra coherent handler memory operation", (1, 0, 0), duplicate_effect, finite_semantics)
    def vector(case, scope):
        index = next(i for i, (s, k, d) in enumerate(case["events"][0]) if k == q31.DELIVER)
        s, k, d = case["events"][0][index]; data = list(d); data[1] = 0x41
        case["events"][0][index] = s, k, tuple(data)
    corrupt("wrong priority vector frame", (5, 0, 0), vector, finite_semantics)
    def branch(case, scope):
        next(a for a in case["attempts"][0] if a["instruction"] == 12 and a["rip"] >= 0x10003500)["rip"] += 16
    corrupt("unrecorded branch through STI shadow", (4, 0, 0), branch, finite_semantics)
    def rep_byte(case, scope):
        step = next(s for s in case["steps"][0] if s["action"] == 9 and not s["operation"]["end"][2])
        data = list(step["operation"]["begin"]); data[0] += 1; step["operation"]["begin"] = tuple(data)
    corrupt("REP duplicated destination byte", (10, 0, 0), rep_byte, finite_semantics)
    def restore(case, scope):
        step = next(s for s in case["steps"][0] if s["action"] == 6 and s["operation"]["begin"][:3] == (0x1000C220, 0, 8))
        data = list(step["operation"]["end"]); data[1] ^= 1; step["operation"]["end"] = tuple(data)
    corrupt("handler restored wrong saved operand", (1, 0, 0), restore, finite_semantics)
    for key in ((1, 0, 0), (2, 0, 0), (7, 2, 0)):
        selected, scoped = {key: cases[key]}, {key: apic_cases[key]}
        native_semantics(selected, scoped, records, operations, header["symbols"], ncpus, halt_trials=True)
        bad = copy.deepcopy(scoped)
        seq, kind, data = next(item for item in bad[key]["events"][0] if item[1] == NATIVE)
        index = bad[key]["events"][0].index((seq, kind, data))
        changed = list(data); changed[2] = header["symbols"]["_q32_hold_end"]
        bad[key]["events"][0][index] = seq, kind, tuple(changed)
        try:
            native_semantics(selected, bad, records, operations, header["symbols"], ncpus, halt_trials=True)
        except ValueError:
            rejected.append("native interior/HLT PC falsifier " + str(key[0]))
        else:
            raise AssertionError("native interval corruption accepted")
    key = (2, 0, 0)
    selected = {key: copy.deepcopy(cases[key])}
    step = next(s for s in selected[key]["steps"][0] if s["action"] == 2)
    step["operation"]["sequence"] = selected[key]["start"]
    try:
        native_semantics(selected, {key: apic_cases[key]}, records, operations, header["symbols"], ncpus, halt_trials=True)
    except ValueError:
        rejected.append("IRQ outside actual XADD operand interval")
    else:
        raise AssertionError("XADD interior corruption accepted")
    diagnostic_logs = []
    for case in sorted(apic_cases.values(), key=lambda c: c["serial"]):
        for _, kind, data in case["events"][0]:
            if kind == CONTROL and data[0] == 14:
                completion = next(d for _, k, d in case["events"][0] if k == CONTROL and d[0] == 2)
                diagnostic_logs.append(dict(q32_diagnostic_stop="masked-halt", cpu=0,
                    **dict(zip(("sequence", "case", "flags", "rip", "irr_word1", "halted"), data[1:])), native_count=completion[4]))
    diagnostic_semantics(diagnostic_logs, 0, apic_cases)
    for name, field in (("external HLT observer wrong PC", "rip"), ("external HLT observer wrong acknowledgement", "sequence"),
                        ("external HLT observer fabricated wake", "halted")):
        bad = copy.deepcopy(diagnostic_logs); bad[0][field] ^= 1
        try:
            diagnostic_semantics(bad, 0, apic_cases)
        except ValueError:
            rejected.append(name)
        else:
            raise AssertionError("external observer corruption accepted")
    key = (12, 0, 0)
    for name, change in (("stale fresh-dispatch generation", lambda a: a.update(generation=a["generation"] - 1)),
                         ("stale native code result", lambda a: a.update(instruction=1))):
        selected = {key: copy.deepcopy(cases[key])}
        attempt = [a for a in selected[key]["attempts"][0] if a["instruction"] == 5][-1]
        change(attempt)
        try:
            code_semantics(selected, {key: apic_cases[key]}, records, ncpus, native_protocol=False)
        except ValueError:
            rejected.append(name)
        else:
            raise AssertionError("code lifetime corruption accepted")
    rejected.extend(trial_controls(cases[7, 2, 0], apic_cases[7, 2, 0], header["symbols"]))
    return rejected


def trial_controls(case, scope, symbols):
    rejected = []
    count = halt_trial_semantics(case, scope, 0, symbols)
    def reject(name, change):
        c, a = copy.deepcopy(case), copy.deepcopy(scope)
        change(c, a)
        try:
            halt_trial_semantics(c, a, 0, symbols)
        except (ValueError, StopIteration):
            rejected.append(name)
        else:
            raise AssertionError("HLT real-trial corruption accepted: " + name)
    def marker(scope, index, field, value):
        indices = [i for i, (_, k, d) in enumerate(scope["events"][0]) if k == CONTROL and d[0] == 15]
        i = indices[index]; s, k, d = scope["events"][0][i]; d = list(d); d[field] = value
        scope["events"][0][i] = s, k, tuple(d)
    reject("HLT skipped ordinal", lambda c, a: marker(a, 0, 1, 2))
    reject("HLT forged required native PC", lambda c, a: marker(a, -1, 3, symbols["_q32_wait_after"] + 4))
    reject("HLT false success without matching native ELR", lambda c, a: marker(a, -1, 2, symbols["_q32_wait_begin"]))
    reject("HLT no qualified final trial", lambda c, a: marker(a, -1, 4, 0))
    reject("HLT wrong post-instruction guest RIP", lambda c, a: marker(a, 0, 5, 0x10003500))
    reject("HLT wrong native acknowledgement", lambda c, a: marker(a, 0, 6, 0))
    reject("HLT discarded actual early native IRQ", lambda c, a: a["events"][0].pop(next(i for i, e in enumerate(a["events"][0]) if e[1] == NATIVE)))
    reject("HLT ignored actual early EOI", lambda c, a: a["events"][0].pop(next(i for i, e in enumerate(a["events"][0]) if e[1] == EOI)))
    reject("HLT ignored actual early IRET", lambda c, a: c["events"][0].pop(next(i for i, e in enumerate(c["events"][0]) if e[1] == q31.RETURN)))
    if count > 1:
        def continued(c, a):
            marker(a, 0, 2, symbols["_q32_wait_after"]); marker(a, 0, 4, 1)
            i = next(i for i, e in enumerate(a["events"][0]) if e[1] == NATIVE)
            s, k, d = a["events"][0][i]; d = list(d); d[2] = symbols["_q32_wait_after"]; a["events"][0][i] = s, k, tuple(d)
        reject("HLT continued after coherent earlier qualification", continued)
        reject("HLT missing encoded retry", lambda c, a: c["attempts"][0].pop(next(i for i, x in enumerate(c["attempts"][0]) if x["instruction"] == 16 and x["rip"] == 0x10003501)))
        def bad_jump(c, a):
            attempt = next(x for x in c["attempts"][0] if x["instruction"] == 16 and x["rip"] == 0x10003501)
            op = [s["operation"] for s in c["steps"][0] if s["recipe"]["attempt"] == attempt["identity"] and s["action"] == 7][1]
            op["end"] = (op["end"][0], 0xFC, *op["end"][2:])
        reject("HLT retry wrong actual EB displacement", bad_jump)
        def bad_context(c, a):
            attempt = next(x for x in c["attempts"][0] if x["instruction"] == 16 and x["rip"] == 0x10003501)
            next(s for s in c["states"][0] if s["attempt"] == attempt["identity"] and s["point"] & 255 == 1)["arch"]["rbx"] ^= 1
        reject("HLT retry reset restored unused GPR", bad_context)
        def premature(c, a):
            ret = next(s for s, k, d in c["events"][0] if k == q31.RETURN)
            indices = [i for i, e in enumerate(a["events"][0]) if e[1] == EMIT]
            i = indices[1]; _, k, d = a["events"][0][i]; a["events"][0][i] = ret - 1, k, d
        reject("HLT rearmed before prior IRET", premature)
    if count == 8:
        reject("HLT trial ordinal beyond cap", lambda c, a: marker(a, -1, 1, 9))
        def all_early(c, a):
            marker(a, -1, 2, symbols["_q32_wait_begin"]); marker(a, -1, 4, 0)
            indices = [i for i, e in enumerate(a["events"][0]) if e[1] == NATIVE];i = indices[-1]
            s, k, d = a["events"][0][i];d = list(d);d[2] = symbols["_q32_wait_begin"];a["events"][0][i] = s, k, tuple(d)
        reject("HLT eight coherent early arrivals cannot pass", all_early)
    return rejected


def self_check():
    rejected = []
    def rejects(name, call):
        try:
            call()
        except ValueError:
            rejected.append(name)
            return
        raise AssertionError("Q32 corrupt control accepted: " + name)
    assert eligible({0x41, 0x4F, 0x71}, set(), 0, 0x202, 0) == 0x71
    assert eligible({0x41, 0x4F}, {0x71}, 0, 0x202, 0) == 0
    assert eligible({0x4F, 0x71}, {0x41}, 0, 0x202, 0) == 0x71
    assert eligible({0x4F}, {0x41}, 0, 0x202, 0) == 0
    assert eligible({0x41, 0x71}, set(), 0x50, 0x202, 0) == 0x71
    assert eligible({0x41}, set(), 0x50, 0x202, 0) == 0
    assert eligible({0x71}, set(), 0, 2, 0) == 0
    assert eligible({0x71}, set(), 0, 0x202, 1) == 0
    q30.require((q30.RAM_BYTES_V4, q30.CAPACITY_V4, q30.ALIAS_V4, CPU_WORDS) ==
                (0x34000000, 8388608, 0x74000000, 85), "explicit v4 native layout")
    raw = bytearray(STATE + STATE_BYTES)
    struct.pack_into("<12Q", raw, STATE, MAGIC, 1, 2, 0, 0, 0, 1, 0, 64, 64, 2, 0)
    struct.pack_into("<3Q", raw, STATE + 96 + 82 * 8, 16, 16, 400)
    decoded, decoded_cpus = decode(raw, 2)
    assert decoded_cpus[0]["diagnostic_request"] == decoded_cpus[0]["diagnostic_ack"] == 16
    assert decoded_cpus[0]["diagnostic_case"] == 400 and len(decoded_cpus[0]["sgi_sent"]) == 4
    struct.pack_into("<Q", raw, STATE + 8, 2)
    decode(raw, 2, required_version=2)
    rejects("Q32 format2 confused with legacy format1", lambda: decode(raw, 2, required_version=1))
    struct.pack_into("<Q", raw, STATE + 8, 3)
    rejects("wrong Q32 raw ABI version", lambda: decode(raw, 2))
    struct.pack_into("<Q", raw, STATE + 8, 1)
    struct.pack_into("<Q", raw, STATE + 96 + 2 * CPU_WORDS * 8, 1)
    rejects("nonzero unused Q32 raw CPU slot", lambda: decode(raw, 2))
    del raw
    old = dict(rip=0x10003003, cs=8, rflags=0x302, rsp=0x10005FF9, ss=16)
    frame = interrupt_frame(old, old["rsp"], 8, 14, False)
    assert frame == dict(rsp=0x10005FC8, slots=(0x10003003, 8, 0x302, 0x10005FF9, 16), cs=8, ss=16, rflags=2)
    assert interrupt_frame(dict(old, rflags=0x10302), old["rsp"], 8, 15, False)["slots"][2] == 0x10302
    assert interrupt_frame(old, 0x10007FF7, 8, 14, True)["ss"] == 0
    records = []
    def emit(cpu, subtype, *payload):
        records.append((len(records) + 1, 14, cpu, 0, (subtype, *payload)))
    emit(0, CASE, 1, 0, 0, 1, 1, q30.MASK, 0)
    emit(0, EMIT, TIMER, 0, 0, 1, 0x41, 0, 0)
    emit(0, NATIVE, TIMER, 27, 0x180, 5, 1, 1, 0)
    emit(0, ACCEPT, TIMER, 0, 0, 1, 0x41, 0, 1)
    emit(0, BOUNDARY, 1, old["rip"], 0x202, 0, 0, 0x41, 0)
    emit(0, DISPATCH, 1, 0x41, frame["rsp"], 1, 0, 1, 1)
    emit(0, EOI, 0x41, 0, 0, 1, 0, 0, 0)
    emit(0, END, 1, 1, 0, 0, 1, 1, 1)
    emit(1, END, 1, 1, 0, 0, 0, 0, 0)
    emit(0, CASE, 1, 0, 0, 0, 1, q30.MASK, 0)
    symbols = dict(_start=0x100, _text_end=0x300)
    def check_apic(trace):
        return apic_trace(trace, 2, 27, 32, symbols, 0, finite_cases=False)
    assert check_apic(records)[1][0]["guest_count"] == 1
    for name, index, field, value in (("wrong native INTID", 2, 2, 2), ("native ELR outside retained text", 2, 3, 0x400),
                                      ("omitted native target", 1, 3, 1), ("wrong vector priority", 4, 6, 0x71),
                                      ("duplicate noncoalesced guest delivery", 5, 1, 2), ("EOI wrong ISR vector", 6, 1, 0x71)):
        trace = list(records); data = list(trace[index][4]); data[field] = value
        trace[index] = (*trace[index][:4], tuple(data))
        rejects(name, lambda: check_apic(trace))

    # A complete five-slot external frame/IRET uses the same actual-access
    # validator as raw acceptance. This fixture is synthetic, not VM evidence.
    initial = dict.fromkeys(q31.ARCH, 0)
    initial.update(old, cr2=0xDECAF000)
    frame = interrupt_frame(initial, initial["rsp"], 8, 14, False)
    handler = dict(initial, rip=0x10003400, rsp=frame["rsp"], rflags=frame["rflags"])
    case = dict(events=[[]], states=[[]], steps=[[]])
    sequence = 0
    def event(kind, data):
        nonlocal sequence
        sequence += 1; case["events"][0].append((sequence, kind, data)); return sequence
    def snapshot(point, arch):
        seq = event(q31.STATE_EVENT, (0,) * 7)
        case["states"][0].append(dict(sequence=seq, end_sequence=seq, point=point, attempt=0, arch=dict(arch)))
        return seq
    def access(action, address, value, rip, write=False):
        seq = event(q31.STEP, (0,) * 7)
        operation = dict(begin=(address, int(write), 8, value, 0, 0, 0, 0), end=(seq, value, 0, 0, 0, 0, 0, 0))
        case["steps"][0].append(dict(sequence=seq, action=action, operation=operation, recipe={"rip": rip}))
    snapshot(6, initial)
    interrupt = snapshot(2, initial)
    gate = 0x3400 | (8 << 16) | (0x8E << 40) | (0x1000 << 48)
    access(3, 0x10001410, gate, initial["rip"])
    access(3, 0x10001418, 0, initial["rip"])
    access(3, 0x10010008, 0x00AF9B000000FFFF, initial["rip"])
    for offset, value in enumerate(reversed(frame["slots"])):
        access(4, frame["rsp"] + 32 - offset * 8, value, initial["rip"], True)
    event(q31.DELIVER, (0, 0x41, 0, initial["cr2"], handler["rip"], frame["rsp"], 1))
    snapshot(3 | 256, handler)
    for offset, value in enumerate(frame["slots"]):
        access(5, frame["rsp"] + offset * 8, value, handler["rip"])
    event(q31.RETURN, (0, 0x41, frame["rsp"], initial["rip"], initial["rsp"], 0, 0))
    snapshot(4, initial)
    def check_frame(candidate):
        q31.delivery_semantics(candidate, 0, {interrupt: 0x41}, interrupt_frame)
    check_frame(case)
    bad = copy.deepcopy(case)
    bad["states"][0][1]["arch"]["rax"] = 1
    rejects("early partial-state interrupt frame", lambda: check_frame(bad))
    bad = copy.deepcopy(case)
    push = [s for s in bad["steps"][0] if s["action"] == 4][2]["operation"]
    push["begin"] = (*push["begin"][:3], initial["rflags"] | (1 << 16), *push["begin"][4:])
    rejects("external IRQ incorrectly forced saved RF", lambda: check_frame(bad))
    bad = copy.deepcopy(case)
    bad["steps"][0].pop(next(i for i, s in enumerate(bad["steps"][0]) if s["action"] == 5))
    rejects("IRET omitted actual frame read", lambda: check_frame(bad))
    native_fixtures = {}
    symbols = dict(_q32_hold_begin=100, _q32_hold_end=200, _q32_wait_begin=300, _q32_wait_end=400, _q32_wait_after=350)
    for group, variant, stage in ((6, 0, 1), (8, 0, 2), (8, 1, 3), (8, 2, 4), (12, 0, 6)):
        key = group, variant, 0
        case = dict(states=[[dict(point=2, end_sequence=1)]], events=[[(5, q31.DELIVER, (1, 14, 0, 0, 0, 0, 1))]])
        payload = (PEER if group == 6 else TIMER, 2 if group == 6 else 30, 150, 5, stage, 1, 0)
        scope = dict(events=[[(3, NATIVE, payload)]])
        trace = [(1, q30.GATE, 0, 1, (1,)), (2, q30.FAULT, 0, 1, (1,)), (3, 14, 0, 1, (NATIVE,) + payload)]
        operations = [dict(cpu=0, sequence=1, end_sequence=5, begin=(0x1000C020, 1, 8, 0xC30000002BB8), faults=1, mutations=[(0x28C020, 42, 43, 8)])]
        native_semantics({key: case}, {key: scope}, trace, operations, symbols, 1)
        native_fixtures[key] = (case, scope, trace, operations)
        changed = copy.deepcopy(scope)
        changed["events"][0][0] = (3, NATIVE, payload[:4] + (0,) + payload[5:])
        rejects("native critical stage " + str((group, variant)),
                lambda: native_semantics({key: case}, {key: changed}, trace, operations, symbols, 1))
        if group == 8 and variant == 0:
            rejects("repair IRQ without actual native fault", lambda: native_semantics({key: case}, {key: scope}, trace[:1] + trace[2:], operations, symbols, 1))
        if group == 12 or group == 8 and variant == 1:
            rejects("code/tracking IRQ outside exclusive epoch " + str(group), lambda: native_semantics({key: case}, {key: scope}, trace[1:], operations, symbols, 1))
    local = native_fixtures[(12, 0, 0)]
    ended = copy.deepcopy(local[3]); ended[0]["end_sequence"] = 2
    rejects("local code IRQ after trapped write completion", lambda: native_semantics({(12, 0, 0): local[0]},
            {(12, 0, 0): local[1]}, local[2], ended, symbols, 1))
    first, second = native_fixtures[(8, 2, 0)], native_fixtures[(8, 0, 0)]
    native_semantics({(8, 2, 0): first[0], (8, 0, 1): second[0]},
                     {(8, 2, 0): first[1], (8, 0, 1): second[1]}, second[2], second[3], symbols, 1)
    legacy_key = (7, 2, 0)
    legacy_case = {legacy_key: dict(states=[[]], events=[[]], attempts=[[]], steps=[[]])}
    legacy_irq = (TIMER, 30, symbols["_q32_wait_after"], 5, 5, 1, 0)
    legacy_scope = {legacy_key: dict(events=[[(1, HALT, (1,)), (2, HALT, (2,)),
                                            (3, NATIVE, legacy_irq), (4, HALT, (3,))]])}
    native_semantics(legacy_case, legacy_scope, [], [], symbols, 1)
    changed = copy.deepcopy(legacy_scope)
    changed[legacy_key]["events"][0][2] = (3, NATIVE, legacy_irq[:2] + (symbols["_q32_wait_begin"],) + legacy_irq[3:])
    rejects("legacy format1 still rejects pre-WFE PC", lambda: native_semantics(legacy_case, changed, [], [], symbols, 1))
    rejects("format2 cannot omit bounded trial records", lambda: native_semantics(legacy_case, legacy_scope, [], [], symbols, 1, halt_trials=True))
    print(json.dumps(dict(status="SYNTHETIC CONTROLS PASS", rejected=rejected,
                          unsupported=["full Q32 finite case coverage", "native hardware evidence", "raw envelope"], vm=False)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, nargs="?")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--model-trace", type=Path)
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if args.model_trace:
        header, records, operations, cases, apic_cases, counts, cpus = model_trace(args.model_trace)
        rejected = model_controls(cases, apic_cases, header["ncpus"], records, operations, header)
        print(json.dumps(dict(status="SYNTHETIC SEMANTICS PASS", cases=len(cases), operations=len(operations), applicability=header["checked_applicability"], rejected=rejected,
                              unsupported=["finite native coverage", "full native protocol", "raw acceptance"], vm=False)))
        return
    if args.run:
        print(json.dumps(q30.check_run(args.run, continuation=check, version=4), indent=2, sort_keys=True))
        return
    parser.error("provide a retained run or explicit synthetic control mode")


if __name__ == "__main__":
    main()

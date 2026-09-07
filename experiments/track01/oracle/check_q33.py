#!/usr/bin/env python3
"""Independent Q33 register-transfer/code-publication oracle; never starts a VM."""
import argparse
import copy
import json
from pathlib import Path
import struct

import check_q30 as q30
import check_q31 as q31
import check_q32 as q32

STATE, MAGIC = 0xE20000, 0x513333434F444531
POOL, POOL_END, OBJECTS, MAX_BYTES, CHUNK = 0x40400000, 0x40700000, 4096, 8192, 32
HEADER = "magic version ncpus failure phase cases checkpoint_records checkpoint_epoch allocated pool_bytes pool_exhaustions".split()
CPU = ("started done cases transaction requests chunks acknowledgements rejections publishes entries effects links timer_start timer_end phase progress "
       "group variant repetition current depth faults interrupts restarts waiting release observed source_generation partial transport_active allocation_limit").split()
BLOCK = "id address length rip page generation source operand kind link enter finish valid transaction users entered".split()
REQUEST_FIELDS = "rip cr3 page generation source operand kind length identity cpu serial nonce".split()
STATE_BYTES = 8 * (len(HEADER) + 4 * len(CPU) + OBJECTS * len(BLOCK))
CASE, REQUEST, SEND, RECEIVE, ACK, REJECT, PUBLISH, ENTER, EFFECT, LINK, CONTROL, END = range(1, 13)
BEGIN, REQUEST_CHUNK, PULL, CONSUMED, CANCEL = range(1, 6)
OK, WAIT, DONE, BAD = range(4)


class CodeState(q32.CodeState):
    q33 = True

    def __init__(self):
        super().__init__()
        self.frozen_sources = {}
        self.q33_started = False

    def event(self, sequence, kind, cpu, epoch, data, phase, owner, active):
        if kind == 15:
            q30.require(data[0] in range(1, 13), "unknown Q33 metadata event")
            if data[0] == CASE:
                self.q33_started = True
            return False
        if self.q33_started and kind == 13 and data[0] == q31.ATTEMPT:
            _, attempt, identity, rip, instruction, length, width, generation = data
            encoding = {1: b"\x48\x8b\x07", 2: b"\x48\x89\x07", 6: b"\x48\x89\x07", 13: b"\x48\x05"}.get(instruction)
            if encoding is not None:
                context = dict(zip(q30.CONTEXT, self.contexts[cpu])); context.pop("pge")
                translated = q30.walk(self.memory, gva=rip, access="execute", **context)
                q30.require(translated["status"] == "ok", "Q33 instruction recipe has no executable source mapping")
                page = translated["gpa"] & ~4095
                frozen = self.frozen_sources.get((cpu, page))
                if frozen is not None and frozen[0] == generation:
                    offset = translated["gpa"] & 4095
                    actual = frozen[1][offset:offset + length]
                    q30.require((length, width) == ((6, 8) if instruction == 13 else (3, 8))
                                and actual.startswith(encoding), "pinned native recovery recipe differs from immutable source generation")
                    if instruction == 13:
                        add_semantics(int.from_bytes(actual, "little"), 0, 0)
                    return False
                q30.require(instruction != 13, "generated ADD lost its exact pinned source metadata")
        result = super().event(sequence, kind, cpu, epoch, data, phase, owner, active)
        if kind == 13 and data[0] == q31.CODE:
            action, page, old, new, operation, generation, users = data[1:]
            if action == 3:
                self.frozen_sources[cpu, page] = generation, bytes(self.memory[page:page + 4096])
            elif action == 4:
                self.frozen_sources.pop((cpu, page), None)
        return result


def decode(ram, ncpus, version=5):
    q30.require(version in (5, 6) and len(ram) == {5:q30.RAM_BYTES_V5, 6:q30.RAM_BYTES_V6}[version], "Q33 complete1600MiB RAM envelope")
    return decode_state(ram, ncpus, STATE)


def decode_state(ram, ncpus, base=0):
    q30.require(len(ram) >= base + STATE_BYTES, "Q33 truncated raw state")
    header = dict(zip(HEADER, struct.unpack_from(f"<{len(HEADER)}Q", ram, base)))
    q30.require((header["magic"], header["version"], header["ncpus"], header["failure"])
                == (MAGIC, 1, ncpus, 0) and ncpus in (2, 4), "Q33 identity/version/failure")
    q30.require(header["allocated"] <= OBJECTS and header["pool_bytes"] <= POOL_END - POOL, "Q33 immutable pool bounds")
    cpus = []
    offset = base + len(HEADER) * 8
    for cpu in range(4):
        words = struct.unpack_from(f"<{len(CPU)}Q", ram, offset + cpu * len(CPU) * 8)
        if cpu >= ncpus:
            q30.require(not any(words), "unused Q33 CPU state")
        else:
            cpus.append(dict(zip(CPU, words)))
    offset += 4 * len(CPU) * 8
    blocks = [dict(zip(BLOCK, struct.unpack_from(f"<{len(BLOCK)}Q", ram, offset + index * len(BLOCK) * 8)))
              for index in range(OBJECTS)]
    return header, cpus, blocks


def packet(words, cpu):
    q30.require(len(words) == 16 and all(type(word) is int and 0 <= word <= q30.MASK for word in words), "Q33 register packet width/value")
    op, transaction, offset, length = words[:4]
    q30.require(op in range(1, 6) and transaction > 0 and words[15] == cpu
                and words[10] in range(4) and words[11] < 256, "Q33 register packet operation/owner/status")
    q30.require(length <= CHUNK and offset <= MAX_BYTES and words[8] <= MAX_BYTES
                and offset + length <= MAX_BYTES, "Q33 register packet overflow or chunk bounds")
    return dict(op=op, transaction=transaction, offset=offset, length=length,
                payload=struct.pack("<4Q", *words[4:8])[:length], total=words[8], generation=words[9],
                status=words[10], flags=words[11], group=words[12], variant=words[13], rep=words[14], cpu=cpu)


def request(data):
    q30.require(len(data) == 96, "Q33 request is not one complete immutable snapshot")
    result = dict(zip(REQUEST_FIELDS, struct.unpack("<12Q", data)))
    q30.require(result["cpu"] < 4 and result["generation"] > 0 and result["identity"] > 0
                and result["serial"] > 0 and result["nonce"] > 0, "Q33 request identity/dependencies")
    q30.require(result["cr3"] < q30.RAM_SIZE and result["cr3"] & 4095 == 0
                and result["page"] < q30.RAM_SIZE and result["page"] & 4095 == 0,
                "Q33 request physical source/address-space identity")
    q30.require(result["kind"] in (1, 2, 3) and result["operand"] == (result["source"] >> 16) & 0xFFFFFFFF,
                "host request operand is not derived from actual guest source bytes")
    return result


def add_semantics(source, operand, flags):
    """Finite guest ADD RAX, sign-extended imm32; no host-computed answer oracle."""
    q30.require(source & 65535 == 0x0548 and source <= q30.MASK, "unsupported guest source instruction")
    immediate = (source >> 16) & 0xFFFFFFFF
    immediate = immediate if immediate < 1 << 31 else immediate - (1 << 32)
    right = immediate & q30.MASK
    result = (operand + right) & q30.MASK
    arithmetic = (int(operand + right > q30.MASK) | (4 if bin(result & 255).count("1") % 2 == 0 else 0)
                  | ((operand ^ right ^ result) & 16) | (64 if result == 0 else 0)
                  | (128 if result >> 63 else 0) | (2048 if (~(operand ^ right) & (operand ^ result)) >> 63 & 1 else 0))
    return result, (flags & ~0x8D5 & ~(1 << 16)) | arithmetic


def decode_template(data, address, kind, source):
    """Interpret the finite straight-line A64 subset, checking both callback ABIs."""
    q30.require(80 <= len(data) <= MAX_BYTES and len(data) % 4 == 0 and address % 4 == 0,
                "generated instruction range/alignment")
    initial = {r: ("saved", r) for r in range(31)}
    initial[0], initial[1] = ("input",), ("descriptor",)
    registers, stack, sp, callbacks = dict(initial), {}, 0, []
    def value(register):
        return 0 if register == 31 else registers[register]
    def add(left, right):
        return left + right if isinstance(left, int) else ("add", left, right) if right else left
    for offset in range(0, len(data), 4):
        word = struct.unpack_from("<I", data, offset)[0]
        pc = address + offset
        if word == 0xD503201F:
            continue
        if word & 0x9F000000 == 0x10000000:  # ADR, signed 21-bit PC-relative.
            immediate = ((word >> 29) & 3) | (((word >> 5) & 0x7FFFF) << 2)
            if immediate & (1 << 20):
                immediate -= 1 << 21
            registers[word & 31] = pc + immediate
        elif word & 0xFFE0FFE0 == 0xAA0003E0:  # MOV Xd,Xm (ORR alias).
            registers[word & 31] = value((word >> 16) & 31)
        elif word & 0xFF000000 == 0x91000000:  # ADD Xd,Xn,#imm, including MOV FP,SP.
            destination, left = word & 31, (word >> 5) & 31
            immediate = ((word >> 10) & 4095) << (12 if word & (1 << 22) else 0)
            q30.require(not word & (1 << 23) and destination != 31, "unsupported generated ADD addressing")
            registers[destination] = add(sp if left == 31 else value(left), immediate)
        elif word & 0xFFC00000 == 0xF9400000:  # LDR Xt,[Xn,#scaled immediate].
            base, destination, displacement = (word >> 5) & 31, word & 31, ((word >> 10) & 4095) * 8
            q30.require(value(base) == ("descriptor",) and displacement in (80, 88), "generated load escaped callback descriptor")
            registers[destination] = ("callback", displacement)
        elif word & 0xFFC00000 in (0xA9800000, 0xA9000000, 0xA9400000, 0xA8C00000):
            mode = word & 0xFFC00000
            q30.require((word >> 5) & 31 == 31, "generated stack pair uses foreign base")
            immediate = (word >> 15) & 127
            immediate = (immediate - 128 if immediate & 64 else immediate) * 8
            one, two = word & 31, (word >> 10) & 31
            if mode == 0xA9800000:
                sp += immediate
            location = sp + (immediate if mode in (0xA9000000, 0xA9400000) else 0)
            if mode in (0xA9800000, 0xA9000000):
                q30.require(location >= -32 and location + 16 <= 0, "generated stack store escaped 32-byte frame")
                stack[location], stack[location + 8] = value(one), value(two)
            else:
                q30.require(location in stack and location + 8 in stack, "generated restore has no matching saved pair")
                registers[one], registers[two] = stack[location], stack[location + 8]
            if mode == 0xA8C00000:
                sp += immediate
        elif word & 0xFFFFFC1F == 0xD63F0000:  # BLR callback only.
            function = value((word >> 5) & 31)
            expected = ("callback", 80 if not callbacks else 88)
            q30.require(len(callbacks) < 2 and function == expected and registers[0] == ("descriptor",)
                        and registers[1] == address and sp == -32, "generated callback control-flow/ABI mismatch")
            callbacks.append(dict(pc=pc, lr=pc + 4, argument=registers[2]))
            for r in range(19):
                registers[r] = ("clobbered", len(callbacks), r)
            registers[0] = ("enter_result",) if len(callbacks) == 1 else ("effect_result",)
            registers[30] = pc + 4
        elif word == 0xD65F03C0:
            q30.require(offset == len(data) - 4 and sp == 0 and all(registers[r] == initial[r] for r in (19, 20, 29, 30))
                        and registers[0] == ("effect_result",), "generated return corrupted native ABI or returned host answer")
        else:
            q30.require(False, f"unimplemented generated instruction {word:#x} at {offset:#x}")
    q30.require(len(callbacks) == 2 and callbacks[0]["argument"] == ("input",)
                and callbacks[0]["lr"] == address + 40 and callbacks[1]["lr"] == address + len(data) - 12
                and struct.unpack_from("<I", data, len(data) - 4)[0] == 0xD65F03C0, "generated block omitted entry/effect/return")
    if kind == 1:
        q30.require(source & 65535 == 0x0548 and (source >> 16) & 0xFFFFFFFF <= 4095, "generated ADD source outside finite ISA")
        immediate = (source >> 16) & 0xFFFFFFFF
    else:
        q30.require(kind in (2, 3) and source == {2: 0x078B48, 3: 0x078948}[kind], "generated memory source outside finite ISA")
        immediate = 0
    q30.require(callbacks[1]["argument"] == add(("enter_result",), immediate), "native arithmetic does not implement exported guest source")
    return callbacks


def copied_response(audit, response, transaction):
    q30.require(len(audit) == 4 and [d[1] for s, d in audit] == list(range(4))
                and all(d[0] == 11 and d[6] == transaction for s, d in audit)
                and [word for s, d in audit for word in d[2:6]] == list(response),
                "copied interrupted WAIT packet differs from all16 host-set response registers")


def transport(transfers, records, ncpus, prefix_records=0, native=False, symbols=None, initial_transactions=None):
    """Replay stopped-owner packets; consumption is bound to guest copy and ACK events."""
    by_cpu = [[] for _ in range(ncpus)]
    for item in transfers:
        q30.require(type(item.get("cpu")) is int and item["cpu"] < ncpus, "transport transcript CPU")
        by_cpu[item["cpu"]].append(item)
    events = [[(s, tuple(d[1:])) for s, k, c, e, d in records
               if s > prefix_records and k == 15 and c == cpu and d[0] == CONTROL and d[1] in (1, 2)]
              for cpu in range(ncpus)]
    indexed, copied_audits, helper_starts = {}, {}, {}
    gate_owner, active_native, inflight = None, set(), set()
    for s, k, c, e, d in records:
        if k == q30.GATE:
            gate_owner = None if d[0] == 0 else d[1]
        elif k == q30.BEGIN:
            inflight.add(c)
        elif k == q30.END:
            inflight.discard(c)
        elif k == q30.MARK and d[0] == 6:
            (active_native.add if d[1] else active_native.discard)(c)
        if k == 15 and d[:2] == (CONTROL, 1):
            helper_starts[c] = s
        elif k == 15 and d[:2] == (CONTROL, 11):
            q30.require(c in helper_starts, "copied packet audit outside actual helper")
            copied_audits.setdefault((c, helper_starts[c]), []).append((s, d[1:]))
        elif k == 15 and d[:2] == (CONTROL, 2):
            helper_starts.pop(c, None)
        if native and k == 15 and d[:2] == (CONTROL, 1):
            q30.require(c not in inflight and c not in active_native and gate_owner != c,
                        "stopped transport owner retains active request or exclusive epoch")
        if s <= prefix_records or k != 15:
            continue
        subtype = d[0]
        if subtype in (SEND, RECEIVE, REQUEST, ACK, REJECT):
            indexed.setdefault((c, subtype, d[1]), []).append((s, d))
        elif subtype == CONTROL and d[1] == 3:
            indexed.setdefault((c, CONTROL, d[2]), []).append((s, d))
    transactions = {}
    for cpu in range(ncpus):
        q30.require(len(events[cpu]) == 2 * len(by_cpu[cpu]), "host exchange count does not match actual guest helper calls")
        current, last_transaction = None, (initial_transactions[cpu] if initial_transactions else 0)
        for index, item in enumerate(by_cpu[cpu]):
            before, after = events[cpu][index * 2:index * 2 + 2]
            a, b = item["request"], item["response"]
            q30.require(len(a) == len(b) == 16 and all(type(v) is int and 0 <= v <= q30.MASK for v in a + b),
                        "host register transcript width/value")
            request_packet = packet(a, cpu)
            op, txn, offset, length = a[:4]
            q30.require(before[1][:4] == (1, op, txn, offset) and after[1][:5] == (2, *b[:3], b[10])
                        and before[0] < after[0], "register packet not bound to exact guest helper interval")
            # A writer lock held by a peer is harmless: this stopped owner must
            # have no active native request and cannot itself own the lock.
            if native:
                q30.require(item.get("register_checks_pass") is True and item["resume_pc"] == item["instruction_pc"] + 4
                            and item["instruction_pc"] == symbols["_q33_transfer_exit"]
                            and (item["get_count"], item["set_count"], item["api_status"]) == (16, 16, 0),
                            "actual stopped-vCPU transfer instruction/register evidence")
            audit = copied_audits.get((cpu, before[0]), [])
            if a[12] == 6 and op == PULL and b[10] == WAIT or audit:
                copied_response(audit, b, txn)
                q30.require(before[0] < audit[0][0] < audit[-1][0] < after[0], "copied WAIT packet evidence outside matching transfer interval")
            unchanged = [0, 8, 9, 11, 12, 13, 14, 15]
            q30.require(all(a[j] == b[j] for j in unchanged), "host response changed immutable request identity/dependencies")
            q30.require(b[10] in (OK, WAIT, DONE, BAD) and item["accepted"] is True,
                        "host setter/protocol failure cannot be consumed as successful transport")
            if op == BEGIN:
                q30.require(current is None and txn == last_transaction + 1 and (cpu, txn) not in transactions and (offset, length, a[8]) == (0, 0, 96)
                            and b == a[:10] + [OK] + a[11:], "transaction begin reused ownership or malformed response")
                current = dict(cpu=cpu, transaction=txn, flags=a[11], generation=a[9], scope=tuple(a[12:15]),
                               start=before[0], stop=None, request_bytes=bytearray(), received=bytearray(), copied=bytearray(),
                               chunks=[], waits=[], exchanges=[], request=None, produced=None, cancelled=False, complete=False)
                transactions[cpu, txn] = current
                last_transaction = txn
            else:
                q30.require(current is not None and current["transaction"] == txn, "chunk has no matching live owner transaction")
                q30.require(tuple(a[12:15]) == current["scope"] and a[9] == current["generation"], "transaction snapshot changed across chunks")
                q30.require(not current.get("invalid") or op == CANCEL, "invalid response reused without cancellation")
                if op == REQUEST_CHUNK:
                    q30.require(offset == len(current["request_bytes"]) and length == CHUNK and offset + length <= 96
                                and a[8] == 96 and b == a[:10] + [OK] + a[11:], "request torn, duplicated, omitted or reordered")
                    sent = [(s, d) for s, d in indexed.get((cpu, SEND, txn), []) if before[0] > s > current["start"]
                            and d[1:4] == (txn, offset, length)]
                    q30.require(len(sent) == 1 and tuple(sent[0][1][4:]) == tuple(a[4:8]), "exported source snapshot differs from stopped registers")
                    current["request_bytes"] += struct.pack("<4Q", *a[4:8])
                    if len(current["request_bytes"]) == 96:
                        recipe = request(current["request_bytes"])
                        q30.require((recipe["identity"], recipe["cpu"], recipe["generation"]) == (txn, cpu, a[9]), "whole request ownership mismatch")
                        declarations = [d for s, d in indexed.get((cpu, REQUEST, txn), []) if current["start"] > s]
                        q30.require(len(declarations) == 1 and declarations[0][2:] == tuple(recipe[x] for x in
                                    ("rip", "page", "generation", "source", "kind", "length")), "request declaration differs from actual transmitted snapshot")
                        if native:
                            q30.require(item.get("produced_request") == list(struct.unpack("<12Q", current["request_bytes"])),
                                        "actual host compiler used different source/dependency snapshot")
                        current["request"] = recipe
                        q30.require(isinstance(item.get("produced_hex"), str), "actual host-produced object bytes missing")
                        produced = bytes.fromhex(item["produced_hex"])
                        q30.require(len(produced) == recipe["length"], "host-produced object length differs from source request")
                        decode_template(produced, POOL, recipe["kind"], recipe["source"])
                        current["produced"] = produced
                elif op == PULL:
                    recipe = current["request"]
                    q30.require(recipe is not None and not current["complete"] and not current.get("offered")
                                and offset == len(current["copied"]) and a[8] == recipe["length"]
                                and length == min(CHUNK, a[8] - offset), "response requested outside exclusive next-chunk ownership")
                    if b[10] == WAIT:
                        q30.require(b == a[:10] + [WAIT] + a[11:], "WAIT supplied consumable or changed payload")
                        q30.require(not current["waits"] and current["flags"] & (1 | 2 | 128)
                                    and (not current["flags"] & 128 or offset >= CHUNK), "unexpected/repeated WAIT ownership state")
                        current["waits"].append((before[0], after[0], offset))
                    elif b[10] == OK:
                        valid = (b[0], b[1], b[2], b[3], b[8], b[9], b[15]) == (PULL, txn, offset, length, a[8], a[9], cpu)
                        if valid:
                            payload = struct.pack("<4Q", *b[4:8])[:length]
                            q30.require(payload == current["produced"][offset:offset + length], "register response differs from actual host-produced native object")
                            current["offered"] = (offset, length, payload, after[0])
                        else:
                            current["invalid"] = (offset, after[0])
                    else:
                        q30.require(b[10] == BAD, "PULL illegally completed transaction without consumption")
                        current["invalid"] = (offset, after[0])
                elif op == CONSUMED:
                    offer = current.pop("offered", None)
                    q30.require(offer is not None and offer[:2] == (offset, length), "ACK consumed an unoffered/invalid/duplicate chunk")
                    expected_status = DONE if offset + length == a[8] else OK
                    q30.require(b == a[:10] + [expected_status] + a[11:], "ACK status/packet changed ownership")
                    received = [(s, d) for s, d in indexed.get((cpu, RECEIVE, txn), []) if offer[3] < s < before[0]
                                and d[1:4] == (txn, offset, length)]
                    copied = [(s, d) for s, d in indexed.get((cpu, CONTROL, txn), []) if offer[3] < s < before[0]]
                    q30.require(len(received) == len(copied) == 1 and received[0][0] < copied[0][0]
                                and tuple(received[0][1][4:]) == tuple(current["exchanges"][-1][3][4:8])
                                and copied[0][1][4:6] == (length, offset + length), "consumed ACK preceded actual matching guest copy")
                    acknowledgements = [(s, d) for s, d in indexed.get((cpu, ACK, txn), [])
                                        if s > after[0] and d[1:4] == (txn, offset, length)]
                    q30.require(len(acknowledgements) == 1 and acknowledgements[0][1][4:] ==
                                (expected_status, a[8], a[9], 0), "retained guest acknowledgement differs from owning-thread response")
                    current["copied"] += offer[2]
                    current["chunks"].append(dict(offset=offset, length=length, receive=received[0][0], copy=copied[0][0],
                                                   address=copied[0][1][3], block=copied[0][1][6], ack=after[0]))
                    if expected_status == DONE:
                        current["complete"], current["stop"] = True, after[0]
                elif op == CANCEL:
                    q30.require(b == a[:10] + [DONE] + a[11:] and not current["complete"], "cancelled completed or wrong transaction")
                    rejected = [(s, d) for s, d in indexed.get((cpu, REJECT, txn), [])
                                if current.get("invalid", (0, 0))[1] < s < before[0]]
                    q30.require(len(rejected) == 1 and rejected[0][1][3] == offset, "cancelled transaction lacks explicit rejection")
                    if rejected[0][1][2] == 1:
                        q30.require(current.get("invalid"), "invalid-response rejection without invalid response")
                    elif rejected[0][1][2] == 7:
                        q30.require(current["waits"] and current["waits"][-1][2] == offset and not current.get("offered")
                                    and rejected[0][1][4:6] == (0, WAIT), "orderly cancellation lacked a real waiting transaction")
                    else:
                        limits = [(s, d) for s, k, c, e, d in records if k == 15 and c == cpu and d[:2] == (CONTROL, 6)
                                  and current["start"] < s < rejected[0][0]]
                        q30.require(rejected[0][1][2] == 6 and not current["copied"] and len(limits) == 1
                                    and limits[0][1][2] <= limits[0][1][3], "allocation rejection lacks actual exhausted finite limit")
                    current["cancelled"], current["stop"] = True, after[0]
                else:
                    q30.require(False, "unimplemented transport request")
            current["exchanges"].append((before[0], after[0], tuple(a), tuple(b)))
            if current["complete"] or current["cancelled"]:
                current = None
        q30.require(current is None, "unfinished stopped-owner transaction")
    return transactions


def scope_trace(records, ncpus, prefix_records=0, finite=True):
    cases, active = {}, None
    variants = dict(enumerate((2, 2, 2, 2, 3, 3, 6, 3, 2, 3), 1))
    for sequence, kind, cpu, epoch, data in records:
        if sequence <= prefix_records or kind != 15:
            continue
        subtype, payload = data[0], data[1:]
        q30.require(subtype in range(1, 13) and cpu < ncpus, "unknown Q33 event")
        if subtype == CASE:
            group, variant, rep, start, serial, count, zero = payload
            key = group, variant, rep
            q30.require(cpu == 0 and group in variants and 0 <= variant < variants[group] and rep < 16
                        and start in (0, 1) and count == ncpus and zero == 0, "Q33 case scope/identity")
            if start:
                q30.require(active is None and key not in cases and serial == len(cases) + 1, "Q33 repeated/nested case")
                cases[key] = dict(start=sequence, stop=None, serial=serial, events=[[] for _ in range(ncpus)], ends={})
                active = key
            else:
                q30.require(active == key and set(cases[key]["ends"]) == set(range(ncpus)), "Q33 case ended before every CPU")
                cases[key]["stop"], active = sequence, None
        elif subtype == CONTROL and payload[0] == 7:
            q30.require(active is None and payload[2:6] == (POOL, POOL_END, OBJECTS, MAX_BYTES), "Q33 native configuration outside startup")
        else:
            q30.require(subtype != CONTROL or payload[0] in range(1, 12), "unimplemented Q33 control action")
            q30.require(active is not None, "Q33 metadata outside active finite case")
            cases[active]["events"][cpu].append((sequence, subtype, payload))
            if subtype == END:
                serial, group, variant, rep, entries, effects, timer = payload
                q30.require((group, variant, rep) == active and serial == cases[active]["serial"]
                            and cpu not in cases[active]["ends"], "Q33 END scope/duplicate")
                cases[active]["ends"][cpu] = (sequence, payload)
    required = {(g, v, r) for g, count in variants.items() for v in range(count) for r in range(16)}
    q30.require(active is None and (set(cases) == required if finite else bool(cases)), "missing Q33 finite cases")
    return cases


def projections(records, ncpus, prefix_records):
    paging, metadata = [], []
    for sequence, kind, cpu, epoch, data in records:
        original = (sequence, kind, cpu, epoch, data)
        if sequence > prefix_records and kind == 15 and data[0] == CASE:
            group, variant, rep, start, serial, count, zero = data[1:]
            paging.append((sequence, 14, cpu, epoch, (q32.CASE, group, variant, rep, start, serial, q30.MASK, 0)))
            metadata.append((sequence, 13, cpu, epoch, (q31.CASE, group, variant, rep, start, serial, count, zero)))
        elif sequence > prefix_records and kind == 15 and data[0] == END:
            serial, group, variant, rep = data[1:5]
            # These ignored counter fields are never evidence. Q33's own END
            # counters are checked separately against actual ENTER/EFFECT events.
            paging.append((sequence, 14, cpu, epoch, (q32.END, serial, group, variant, rep, 0, 0, 0)))
            metadata.append((sequence, 13, cpu, epoch, (q31.END, serial, group, variant, rep, q30.MASK, q30.MASK, 0)))
        else:
            if kind == 14:
                paging.append(original)
            if kind == 13:
                metadata.append(original)
    return paging, metadata


def objects_semantics(transactions, scopes, cases, records, ncpus, pool, final_blocks=None, symbols=None, native=False, initial_cache_lines=None, initial_blocks=None):
    """Immutable native bytes, source generations, actual callbacks and canonical effects."""
    q30.require(len(pool) == POOL_END - POOL, "complete generated-code pool artifact missing")
    block_requests, allocated, published, active, entered = {}, {}, {}, {}, [None] * ncpus
    generations, roles, pins = [0] * 1024, [0] * 1024, {}
    totals = [dict(requests=0, chunks=0, acknowledgements=0, rejections=0, publishes=0, entries=0, effects=0, links=0) for _ in range(ncpus)]
    for key, tx in transactions.items():
        totals[key[0]]["requests"] += 1
        totals[key[0]]["chunks"] += len(tx["chunks"])
        totals[key[0]]["acknowledgements"] += len(tx["chunks"])
        ids = {chunk["block"] for chunk in tx["chunks"]}
        q30.require(len(ids) <= 1, "one transport transaction copied into multiple native objects")
        if ids:
            identity = ids.pop()
            q30.require(identity not in block_requests, "native object overwritten by another transaction")
            address = tx["chunks"][0]["address"]
            q30.require(all(c["address"] == address + c["offset"] for c in tx["chunks"]), "native chunk copy escaped immutable destination")
            block_requests[identity] = tx
            allocated[identity] = dict(address=address, length=tx["request"]["length"], transaction=key)
            start = address - POOL
            q30.require(0 <= start <= len(pool) - len(tx["copied"]) and pool[start:start + len(tx["copied"])] == tx["copied"],
                        "actual retained native code bytes differ from register-delivered guest copies")
    # Index every case's shared canonical snapshots once.
    def fetched(recipe, cpu, key, before, after=0, request_context=True):
        length = 8 if recipe["kind"] == 1 else 3
        steps = [s for s in cases[key]["steps"][cpu] if after < s["sequence"] < before and s["action"] == 7][-length:]
        q30.require(len(steps) == length and len({s["recipe"]["attempt"] for s in steps}) == 1,
                    "native object dependency lacks complete actual source-byte fetch")
        actual = bytearray()
        for index, step in enumerate(steps):
            op = step["operation"]
            q30.require(op["begin"][:3] == (recipe["rip"] + index, 4, 1) and op["end"][2] == 0
                        and op["end_walk"]["gpa"] & ~4095 == recipe["page"]
                        and (not request_context or op["context"][0] == recipe["cr3"]),
                        "native object source fetch used wrong mapping/address-space or permission")
            actual.append(op["end"][1])
        q30.require(int.from_bytes(actual, "little") == recipe["source"], "native source snapshot differs from actual fetched guest bytes")
    for tx in transactions.values():
        fetched(tx["request"], tx["cpu"], tx["scope"], tx["start"])
    states = {(key, cpu): case["states"][cpu] for key, case in cases.items() for cpu in range(ncpus)}
    attempts = {(key, cpu): case["attempts"][cpu] for key, case in cases.items() for cpu in range(ncpus)}
    link_updates = {}
    for s, k, c, e, d in records:
        if k == 15 and d[:2] == (LINK, 1):
            link_updates.setdefault(d[2], []).append((s, d[3], d[4]))
    def visible_links(identity, sequence):
        current = published[identity]["link"]
        following = next((new for s, old, new in link_updates.get(identity, []) if s > sequence), None)
        return {current} if following is None else {current, following}
    current_scope, cache_lines = None, dict(initial_cache_lines or {})
    for sequence, kind, cpu, epoch, data in records:
        if kind == 13 and data[0] == q31.CODE:
            action, page, old, new, operation, generation, users = data[1:]
            if action == 1: roles[page // 4096] = new
            elif action == 2: generations[page // 4096] = new
            elif action == 3:
                q30.require((cpu, page) not in pins, "duplicate source metadata pin")
                pins[cpu, page] = (sequence, generation)
            elif action == 4:
                q30.require((cpu, page) in pins, "source metadata unpin without owner")
                del pins[cpu, page]
        if kind != 15:
            continue
        subtype, d = data[0], data[1:]
        if subtype == CASE:
            current_scope = tuple(d[:3]) if d[3] else None
            continue
        if subtype == CONTROL and d[0] == 7:
            q30.require(cpu not in cache_lines and d[2:6] == (POOL, POOL_END, OBJECTS, MAX_BYTES), "native publication configuration mismatch")
            cache_lines[cpu] = (4 << ((d[1] >> 16) & 15), 4 << (d[1] & 15))
        elif subtype == PUBLISH:
            identity, address, length, generation, dc, ic, txn = d
            tx = transactions.get((cpu, txn))
            q30.require(tx is not None and tx["complete"] and not tx["cancelled"] and tx["stop"] < sequence
                        and identity in allocated and identity not in published and allocated[identity]["address"] == address
                        and tx["request"]["length"] == length and tx["request"]["generation"] == generation
                        and len(tx["copied"]) == length, "partial, stale or foreign native publication")
            fetched(tx["request"], cpu, current_scope, sequence, tx["stop"])
            page = tx["request"]["page"]
            q30.require(generations[page // 4096] == generation and roles[page // 4096], "native object published after source-generation retirement")
            q30.require(all(4 <= line <= 2048 and line & (line - 1) == 0 for line in (dc, ic)), "cache maintenance line geometry")
            q30.require((dc, ic) == cache_lines.get(cpu), "publication cache lines differ from retained CTR_EL0")
            decode_template(tx["copied"], address, tx["request"]["kind"], tx["request"]["source"])
            published[identity] = dict(sequence=sequence, cpu=cpu, tx=tx, address=address, length=length, generation=generation,
                                       link=0, entries=0, effects=0)
            totals[cpu]["publishes"] += 1
        elif subtype == CONTROL and d[0] == 4:
            _, identity, address, generation, owner, depth, zero = d
            obj = published.get(identity)
            q30.require(obj is not None and owner == cpu and 1 <= depth <= 3 and zero == 0
                        and (address, generation) == (obj["address"], obj["generation"]), "entry ISB before complete publication or wrong object")
            fetched(obj["tx"]["request"], cpu, current_scope, sequence, obj["sequence"], request_context=False)
            page = obj["tx"]["request"]["page"]
            q30.require(pins.get((cpu, page), (None, None))[1] == generation and generations[page // 4096] == generation,
                        "entry lacks exact live source-generation pin")
            q30.require(cpu not in active, "nested native entry before previous effect")
            active[cpu] = dict(identity=identity, start=sequence, enter=None, key=current_scope)
        elif subtype == ENTER:
            identity, pc, lr, value, generation, users, link = d
            obj = published.get(identity)
            pending = active.get(cpu)
            q30.require(obj is not None and pending is not None and pending["identity"] == identity and pending["enter"] is None
                        and pending["start"] < sequence and (pc, lr, generation) ==
                        (obj["address"], obj["address"] + 40, obj["generation"]) and link in visible_links(identity, sequence)
                        and 1 <= users <= ncpus, "actual generated entry PC/LR/ownership/link mismatch")
            pending.update(enter=sequence, input=value)
            totals[cpu]["entries"] += 1; obj["entries"] += 1
        elif subtype == EFFECT:
            identity, lr, value, result, rip, flags, generation = d
            obj, pending = published.get(identity), active.pop(cpu, None)
            q30.require(obj is not None and pending is not None and pending["identity"] == identity and pending["enter"] is not None
                        and (lr, value, generation) == (obj["address"] + obj["length"] - 12, pending["input"], obj["generation"]),
                        "actual native effect skipped entry/arithmetic or used stale descriptor")
            recipe = obj["tx"]["request"]
            own = [s for s in states[pending["key"], cpu] if pending["enter"] < s["sequence"] < sequence]
            before = next((s for s in own if s["point"] & 255 == 1), None)
            commits = [s for s in own if s["point"] & 255 == 5]
            q30.require(before is not None and len(commits) == 1 and before["arch"]["rax"] == value
                        and before["arch"]["rip"] == recipe["rip"], "generated entry lacks exact guest canonical attempt/one effect")
            expected = dict(before["arch"])
            if recipe["kind"] == 1:
                expected["rax"], expected["rflags"] = add_semantics(recipe["source"], value, expected["rflags"])
                expected["rip"] += 6
                add_retirement(recipe["source"], before["arch"], commits[0]["arch"])
                own_attempts = [a for a in attempts[pending["key"], cpu] if pending["enter"] < a["sequence"] < sequence]
                q30.require(len(own_attempts) == 1 and (own_attempts[0]["instruction"], own_attempts[0]["length"], own_attempts[0]["width"])
                            == (13, 6, 8), "generated ADD lost immutable instruction recovery recipe")
            # LOAD and its actual fault/frame/IRET/retry are independently checked
            # by the shared scalar and delivery oracle, including all other GPRs.
            q30.require((result, rip, flags) == tuple(commits[0]["arch"][x] for x in ("rax", "rip", "rflags")),
                        "native result callback differs from actual guest canonical retirement")
            totals[cpu]["effects"] += 1; obj["effects"] += 1
        elif subtype == LINK:
            action, source, old, target, changed_epoch, mask, zero = d
            q30.require(source in published and zero == 0, "link names unpublished source")
            obj = published[source]
            if action == 1:
                q30.require(target in published or target == 0, "link names unpublished target")
                q30.require(obj["link"] == old and mask == 0, "link replacement old-target/publication mismatch")
                obj["link"] = target
            elif action == 2:
                q30.require(old == target and target in published and target in visible_links(source, sequence)
                            and mask == 1 << cpu, "linked dispatch did not select an atomically visible published target")
                own = states[current_scope, cpu]
                prior = [s for s in own if s["sequence"] < sequence]
                q30.require(prior and prior[-1]["point"] & 255 == 5
                            and prior[-1]["arch"]["rip"] == published[target]["tx"]["request"]["rip"],
                            "native link skipped actual guest branch retirement")
                totals[cpu]["links"] += 1
            else:
                q30.require(action == 3 and old == target == 0 and mask == 1 << cpu and cpu not in active,
                            "native link acknowledgement owner/bit mismatch")
        elif subtype == REJECT:
            totals[cpu]["rejections"] += 1
        elif subtype == END:
            q30.require((d[4], d[5]) == (totals[cpu]["entries"], totals[cpu]["effects"]), "Q33 END invented native entries/effects")
    q30.require(not active and all(o["entries"] == o["effects"] for o in published.values()), "unfinished generated native call")
    if final_blocks is not None:
        ranges = []
        for index, block in enumerate(final_blocks):
            if not block["id"]:
                q30.require(not any(block.values()), "unused immutable object descriptor is nonzero")
                continue
            identity = block["id"]
            if initial_blocks and initial_blocks[index]["id"]:
                q30.require(block == initial_blocks[index], "earlier immutable descriptor changed in Q34")
                low = block["address"] - 20
                ranges.append((low, low + ((block["length"] + 83) & ~63)))
                continue
            q30.require(identity == index + 1 and block["address"] % 64 == 20 and 80 <= block["length"] <= MAX_BYTES
                        and block["length"] % 4 == 0 and block["users"] == 0, "final native descriptor identity/alignment/lifetime")
            size = (block["length"] + 83) & ~63
            low = block["address"] - 20
            q30.require(POOL <= low and low + size <= POOL_END, "immutable descriptor escaped code pool")
            ranges.append((low, low + size))
            owners = [tx for tx in transactions.values() if tx["transaction"] == block["transaction"]
                          and tx["request"] is not None and tx["request"]["rip"] == block["rip"]
                          and tx["request"]["source"] == block["source"]
                          and (not tx["chunks"] or tx["chunks"][0]["block"] == identity)]
            q30.require(len(owners) == 1, "unpublished descriptor has ambiguous per-CPU transaction owner")
            owner = owners[0]
            q30.require(owner is not None and all(block[name] == owner["request"][name] for name in
                        ("rip", "page", "generation", "source", "operand", "kind", "length")), "descriptor metadata differs from register-transferred source")
            obj = published.get(identity)
            q30.require(block["valid"] == int(obj is not None) and block["entered"] == (obj["entries"] if obj else 0)
                        and block["link"] == (obj["link"] if obj else 0), "final descriptor publication/entry/link state")
            if native:
                q30.require((block["enter"], block["finish"]) == (symbols["_generated_enter"], symbols["_generated_finish"]),
                            "native callback descriptor points outside retained implementation")
            used = len(owner["copied"])
            start = block["address"] - POOL
            q30.require(pool[start:start + used] == owner["copied"] and not any(pool[start + used:low - POOL + size])
                        and not any(pool[low - POOL:start]), "immutable pool padding or uncopied tail changed")
        ranges.sort()
        q30.require(all(a[1] == b[0] for a, b in zip(ranges, ranges[1:])) and (not ranges or ranges[0][0] == POOL),
                    "native pool allocations overlap or conceal overwrite/gap")
        q30.require(not any(pool[ranges[-1][1] - POOL if ranges else 0:]), "unallocated native pool bytes changed")
    return published, totals


def branches(cases, ncpus, fixture_bases=None):
    for key, case in cases.items():
        for cpu in range(ncpus):
            for attempt in case["attempts"][cpu]:
                if attempt["instruction"] != 16:
                    continue
                own = [s for s in case["states"][cpu] if s["attempt"] == attempt["identity"]]
                before = [s for s in own if s["point"] & 255 == 1]
                final = [s for s in own if s["point"] & 255 == 5]
                steps = [s for s in case["steps"][cpu] if s["recipe"]["attempt"] == attempt["identity"] and s["action"] == 7]
                q30.require(len(before) == len(final) == 1 and len(steps) == 2 and (attempt["length"], attempt["width"]) == (2, 1),
                            "native chain lacks one actual guest short JMP")
                for index, step in enumerate(steps):
                    q30.require(step["operation"]["begin"][:3] == (attempt["rip"] + index, 4, 1)
                                and step["operation"]["end"][2] == 0, "guest branch opcode not fetched with execute permission")
                opcode, displacement = [s["operation"]["end"][1] for s in steps]
                q30.require(opcode == 0xEB and displacement <= 255, "native link used unsupported guest branch encoding")
                expected = dict(before[0]["arch"])
                expected["rip"] = (expected["rip"] + 2 + (displacement - 256 if displacement & 128 else displacement)) & q30.MASK
                expected["rflags"] &= ~(1 << 16)
                q30.require(final[0]["arch"] == expected, "native link changed wrong guest branch/register state")
                if (attempt["rip"] & 0x1FFFF) < 0x4000:
                    q30.require(expected["rip"] == (0x10000000 + cpu * 0x20000 if fixture_bases is None else fixture_bases[key,cpu]) + 0x3460, "guest IRQ handler branch skipped EOI site")


def interrupt_effects(cases, apic_cases, ncpus, fixture_bases=None):
    for key, case in cases.items():
        for cpu in range(ncpus):
            events = apic_cases[key]["events"][cpu]
            dispatches = [(s, d) for s, k, d in events if k == q32.DISPATCH and d[4] == 0]
            stores = [step["operation"] for step in case["steps"][cpu] if step["action"] == 2
                      and step["operation"]["begin"][:3] == ((0x10000000 + cpu * 0x20000 if fixture_bases is None else fixture_bases[key,cpu]) + 0xC100, 1, 8)
                      and step["operation"]["end"][2] == 0]
            q30.require([op["begin"][3] for op in stores] == list(range(1, len(dispatches) + 1)),
                        "guest interrupt handler omitted or duplicated actual memory effect")
            for sequence, data in dispatches:
                boundary = next((s for s, k, d in events if k == q32.BOUNDARY and d[0] == data[0]), None)
                old = next((state for state in case["states"][cpu] if state["sequence"] > boundary), None)
                returned = next(((s, d) for s, k, d in case["events"][cpu] if k == q31.RETURN and s > sequence
                                 and d[1] == data[1] and d[2] == data[2]), None)
                q30.require(old is not None and returned is not None, "guest IRQ handler lacks actual matching frame return")
                after = next((state for state in case["states"][cpu] if state["sequence"] > returned[0]), None)
                q30.require(after is not None and after["point"] & 255 == 4 and after["arch"] == old["arch"],
                            "guest interrupt handler failed to preserve exact canonical continuation state")


def source_probes(cases, ncpus, scopes, transactions):
    for key, case in cases.items():
        for cpu in range(ncpus):
            for attempt in case["attempts"][cpu]:
                if attempt["instruction"] != 5:
                    continue
                own = [s for s in case["states"][cpu] if s["attempt"] == attempt["identity"]]
                before = [s for s in own if s["point"] & 255 == 1]
                q30.require(len(before) == 1 and attempt["width"] == 1 and attempt["length"] in (3, 6),
                            "source probe lacks finite byte-fetch metadata")
                steps = [s for s in case["steps"][cpu] if s["recipe"]["attempt"] == attempt["identity"] and s["action"] == 7]
                dependency_length = 8 if attempt["length"] == 6 else 3
                q30.require(0 < len(steps) <= dependency_length, "source probe omitted all bytes or fetched past instruction")
                for index, step in enumerate(steps):
                    op = step["operation"]
                    q30.require(op["begin"][:3] == (attempt["rip"] + index, 4, 1), "source probe skipped/repeated execute-permission byte")
                    q30.require(op["end"][2] == 0 or index == len(steps) - 1, "source probe continued after guest fetch denial")
                if steps[-1]["operation"]["end"][2]:
                    faults = [s for s in own if s["point"] & 255 == 2]
                    expected = dict(before[0]["arch"], cr2=steps[-1]["operation"]["begin"][0])
                    q30.require(faults and faults[0]["arch"] == expected, "true guest fetch fault lost exact pre-transfer architecture")
                elif len(steps) != dependency_length:
                    following = next((a["sequence"] for a in case["attempts"][cpu] if a["sequence"] > attempt["sequence"]), case["stop"])
                    rejects = [(s, d) for s, kind, d in scopes[key]["events"][cpu]
                               if steps[-1]["sequence"] < s < following and kind == REJECT]
                    q30.require(len(rejects) == 1 and rejects[0][1][1] == 2, f"incomplete source dependency was not rejected: {key} CPU{cpu} {attempt} len{len(steps)}")
                    transaction = transactions.get((cpu, rejects[0][1][0]))
                    q30.require(transaction is not None and transaction["complete"]
                                and steps[-1]["operation"]["end_walk"]["gpa"] & ~4095 != transaction["request"]["page"],
                                "incomplete source fetch lacks actual physical-mapping mismatch")
                q30.require(not any(s["point"] & 255 == 5 for s in own), "translation source probe invented guest instruction retirement")


def semantic_trace(records, operations, ncpus, header, transfers, pool, blocks=None, prefix_records=0,
                   initial_q31=None, initial_q32=None, native=False):
    scopes = scope_trace(records, ncpus, prefix_records)
    apic_records, metadata_records = projections(records, ncpus, prefix_records)
    variants = dict(enumerate((2, 2, 2, 2, 3, 3, 6, 3, 2, 3), 1))
    apic_cases, apic = q32.apic_trace(apic_records, ncpus, header["timer_intid"], header["spi_base"], header["symbols"], prefix_records,
                                     variant_counts=variants, initial_states=initial_q32, scope_end_counters=False, max_native_stage=7)
    cases, counts = q31.index_trace(metadata_records, operations, ncpus, prefix_records, variants, initial_q31, interrupt_mode=True, live_pin_recipes=True)
    del metadata_records, apic_records
    transactions = transport(transfers, records, ncpus, prefix_records, native, header["symbols"])
    source_probes(cases, ncpus, scopes, transactions)
    q32.guest_semantics(cases, apic_cases, ncpus, additional_instructions=(5, 13, 16), nonretiring_probes=(5,))
    branches(cases, ncpus)
    interrupt_effects(cases, apic_cases, ncpus)
    objects, totals = objects_semantics(transactions, scopes, cases, records, ncpus, pool, blocks, header["symbols"], native)
    return dict(scopes=scopes, cases=cases, apic_cases=apic_cases, apic=apic, counts=counts,
                transactions=transactions, objects=objects, totals=totals)


def model_trace(path, host_path, pool_path, state_path=None):
    with path.open() as stream:
        header = json.loads(next(stream))
        q30.require(header["format"] == "natemu-q33-model-trace-v1", "Q33 synthetic trace schema")
        records = [(s, k, c, e, tuple(d)) for s, k, c, e, d in map(json.loads, stream)]
    host = json.loads(host_path.read_text())
    q30.require(host["format"] == "natemu-q33-model-host-v1", "Q33 synthetic stopped-owner transcript schema")
    ncpus = header["ncpus"]
    contexts, pending, operations = [q30.DEFAULT_CONTEXT] * ncpus, {}, []
    for sequence, kind, cpu, epoch, data in records:
        if kind == q30.CONTEXT_EVENT:
            contexts[cpu] = data[:5]
        elif kind == q30.BEGIN:
            q30.require(cpu not in pending, "nested modeled native operation")
            pending[cpu] = dict(sequence=sequence, cpu=cpu, begin=data, context=contexts[cpu])
        elif kind == q30.END:
            operation = pending.pop(cpu)
            q30.require(data[0] == operation["sequence"], "model native operation linkage")
            operation.update(end_sequence=sequence, end=data, end_walk={"gpa": data[4]})
            operations.append(operation)
    q30.require(not pending, "unfinished modeled native operations")
    qheader, cpus, blocks = decode_state(state_path.read_bytes(), ncpus) if state_path else (None, None, None)
    result = semantic_trace(records, operations, ncpus, header, host["transfers"], pool_path.read_bytes(), blocks,
                            initial_q31=header["initial_q31_counts"], initial_q32=header.get("initial_q32_states"))
    if state_path:
        final_state(qheader, cpus, blocks, result, native=False)
    result["coverage"] = finite_semantics(result, records, operations, ncpus)
    return header, records, operations, result, (qheader, cpus, blocks)


def native_instructions(ram, symbols):
    """Decode the actual fixed register-copy and cache-publication instruction sites."""
    start, stopped, resumed, copied = [symbols[name] for name in
        ("_q33_transfer", "_q33_transfer_exit", "_q33_transfer_resume", "_q33_transfer_copy_end")]
    expected_prefix = [0xA9BE53F3, 0xF9000BFE, 0xAA0003F3]
    expected_prefix += [0xA9400000 | (2 * index << 15) | ((2 * index + 1) << 10) | (19 << 5) | (2 * index) for index in range(8)]
    q30.require(stopped == start + 4 * len(expected_prefix) and resumed == stopped + 4
                and list(struct.unpack_from(f"<{len(expected_prefix)}I", ram, start - 0x40000000)) == expected_prefix
                and struct.unpack_from("<I", ram, stopped - 0x40000000)[0] == 0xD40006C2,
                "native stopped-vCPU request helper does not transfer all16 registers from owned packet")
    response = [0xD50342FF, 0xD5033FDF] + [0xD503201F] * 16
    response += [0xA9000000 | (2 * index << 15) | ((2 * index + 1) << 10) | (19 << 5) | (2 * index) for index in range(8)]
    q30.require(copied == resumed + 4 * len(response)
                and list(struct.unpack_from(f"<{len(response)}I", ram, resumed - 0x40000000)) == response
                and struct.unpack_from("<3I", ram, copied - 0x40000000) == (0xF9400BFE, 0xA8C253F3, 0xD65F03C0),
                "native response helper omitted register copy, unmask/context synchronization or native ABI restoration")
    sites = [symbols["_q33_publish_" + name] for name in ("dc", "dsb1", "ic", "dsb2", "isb")]
    q30.require(sites == sorted(set(sites)), "cache publication native sites reordered or aliased")
    words = [struct.unpack_from("<I", ram, address - 0x40000000)[0] for address in sites]
    q30.require(words[0] & ~31 == 0xD50B7B20 and words[1] == 0xD5033B9F
                and words[2] & ~31 == 0xD50B7520 and words[3:] == [0xD5033B9F, 0xD5033FDF],
                "native publication omitted DC CVAU/DSB ISH/IC IVAU/DSB ISH/ISB")


def stopped_source_witness(cpu, transaction, variant, irq, logs, symbols, arm=None, timer_intid=None):
    low, first_store = symbols["_q33_transfer_resume"] + 4, symbols["_q33_transfer_resume"] + 72
    q30.require(irq[0] == variant + 1 and irq[6] == cpu
                and low <= irq[2] <= first_store < symbols["_q33_transfer_copy_end"] and irq[4] == 7,
                "IRQ source/owner mismatch or arrival after some response registers were copied")
    witness = [r for r in logs if r.get("cpu") == cpu and r.get("transaction") == transaction and "q33_stopped_source" in r]
    q30.require(len(witness) == 2 and [r["q33_stopped_source"] for r in witness] == ["before", "after"]
                and witness[0]["pending"] is False and not any(r["active"] for r in witness)
                and all(r["source"] == variant + 1 and r["intid"] == irq[1] and r["cpsr"] & 128 for r in witness),
                "stopped-source observation lost owner, quiet start or masked unserviced state")
    bit = 1 << (irq[1] % 32)
    q30.require(all(bool(r["pending_word"] & bit) == r["pending"] and bool(r["active_word"] & bit) == r["active"] for r in witness),
                "stopped-source booleans disagree with actual GIC register bits")
    if variant == 0:
        # Arm Generic Timer §§3.3–3.4: enabled, unmasked ISTATUS reports
        # the firing condition. Apple exposes the current system register but
        # does not promise that a separate stopped GIC read already latched it.
        q30.require(timer_intid is not None and irq[1] == timer_intid
                    and arm is not None and len(arm) == 7 and arm[0] == 8 and arm[1] > 0
                    and arm[4:] == (transaction, 0, 0), "timer witness lacks exact guest arm/transaction/INTID")
        q30.require([r["timer_ctl"] & 7 for r in witness] == [1, 5]
                    and witness[0]["timer_cval"] == witness[1]["timer_cval"] == arm[2],
                    "timer did not become enabled/unmasked expired at unchanged guest comparator while stopped")
    else:
        q30.require(witness[1]["pending"] is True, "SGI/SPI did not become GIC pending while owner was stopped")
    return witness


def delayed_source_witness(cpu, transaction, variant, interval, native, events, timing, logs, symbols, timer_intid):
    arms = [(s, d) for s, k, d in events if k == CONTROL and d[0] == 8 and interval[0] < s < native[0]]
    q30.require(len(arms) == 1, "delayed response lacks exact preceding guest arm record")
    accepted = [d for s, k, d in timing if k == q32.ACCEPT and native[0] < s < interval[1] and d[0] == variant + 1]
    q30.require(len(accepted) == 1 and accepted[0][3] == arms[0][1][1],
                "stopped source arm differs from actual native payload acceptance")
    return stopped_source_witness(cpu, transaction, variant, native[1], logs, symbols, tuple(arms[0][1]), timer_intid)


def finite_semantics(result, records, operations, ncpus, native=False, logs=None, symbols=None, partial=False, timer_intid=None):
    """Require each declared trigger and actual continuation, never producer counters alone."""
    scopes, cases, objects, transactions = (result[k] for k in ("scopes", "cases", "objects", "transactions"))
    tx_by_case = {}
    for tx in transactions.values(): tx_by_case.setdefault((tx["scope"], tx["cpu"]), []).append(tx)
    mutations = [(s, c, d) for s, k, c, e, d in records if k == q30.MUTATION]
    generations = [(s, c, d) for s, k, c, e, d in records if k == 13 and d[:2] == (q31.CODE, 2)]
    native_faults = {d[0]: s for s, k, c, e, d in records if k == q30.FAULT}
    covered = 0
    for key, scope in scopes.items():
        group, variant, rep = key
        all_tx = [tx for (own, cpu), row in tx_by_case.items() if own == key for tx in row]
        if group == 4:
            q30.require(len(all_tx) == ncpus and len({tx["request"]["page"] for tx in all_tx}) == (1 if variant else ncpus),
                        "same/different source simultaneous miss fixture is missing")
            if native:
                q30.require(max(tx["start"] for tx in all_tx) < min(tx["stop"] for tx in all_tx), "simultaneous host misses did not actually overlap")
        for cpu in range(ncpus):
            events, guest = scope["events"][cpu], cases[key]
            txs = sorted(tx_by_case.get((key, cpu), []), key=lambda tx: tx["start"])
            enters = [(s, d) for s, kind, d in events if kind == ENTER]
            effects = [(s, d) for s, kind, d in events if kind == EFFECT]
            rejects = [(s, d) for s, kind, d in events if kind == REJECT]
            steps = guest["steps"][cpu]
            if group in (1, 2, 4):
                q30.require(len(enters) == len(effects) == 1 and enters[0][1][3] == 100 + cpu
                            and effects[0][1][3] == 107 + cpu + rep, "host-produced arithmetic operand/result coverage")
                obj = objects[enters[0][1][0]]
                q30.require(obj["length"] == (160 if group == 2 and not variant else 4288 if group == 2 else 80),
                            "generated multi-line/page instruction body missing")
                if group == 1 and variant:
                    q30.require(obj["cpu"] == 0 and len(txs) == int(cpu == 0), "shared native object was privately regenerated per peer")
                else:
                    q30.require(len(txs) == 1 and obj["cpu"] == cpu, "per-owner production identity contaminated")
                if group == 2 and variant:
                    q30.require(obj["address"] // 4096 != (obj["address"] + obj["length"] - 1) // 4096,
                                "large native object did not execute across a page boundary")
            elif group == 3:
                q30.require(len(enters) == len(effects) == (12 if cpu == 0 else 15), "live chain traversal/peer progress count")
                updates = [(s, d) for row in scope["events"] for s, kind, d in row if kind == LINK and d[0] == 1]
                replacement = max(updates, key=lambda item: item[0])
                acknowledgements = [(c, s, d) for c, row in enumerate(scope["events"]) for s, kind, d in row if kind == LINK and d[0] == 3]
                q30.require(len(acknowledgements) == ncpus and {c for c, s, d in acknowledgements} == set(range(ncpus))
                            and all(s > replacement[0] for c, s, d in acknowledgements), "live link replacement lacks every vCPU acknowledgement")
                ack = max(s for c, s, d in acknowledgements)
                own_ack = next(s for c, s, d in acknowledgements if c == cpu)
                if cpu:
                    old_target = replacement[1][2]
                    old_entries = [(s, d) for s, d in enters if d[0] == old_target and s < replacement[0]]
                    q30.require(old_entries and any(replacement[0] < s < own_ack and d[0] == old_target for s, d in effects),
                                "link replaced outside actual peer old-target execution")
                traversals = [(s, d) for s, kind, d in events if kind == LINK and d[0] == 2]
                q30.require(len(traversals) == (8 if cpu == 0 else 10), "resolved chain did not stay entirely inside VM")
                for sequence, d in traversals:
                    source, target = d[1], d[3]
                    following = next(((s, e) for s, e in enters if s > sequence), None)
                    q30.require(following is not None and following[1][0] == target and not any(sequence < tx["start"] < following[0] for tx in txs),
                                "resolved native link caused host exit or entered wrong target")
                    if sequence > ack and source == replacement[1][1]:
                        q30.require(target == replacement[1][3], "post-ack link executed retired old target")
            elif group == 5:
                q30.require(len(txs) == (2 if variant == 2 else 3) and len(enters) == len(effects) == 1,
                            "stale reply/entry did not reject then complete one fresh effect")
                first = txs[0]
                q30.require(first["cancelled"] if variant == 2 else first["complete"], "stale response path was not exercised")
                q30.require(not any(obj["tx"] is first for obj in objects.values()), "stale/duplicate response became executable")
                if variant < 2:
                    q30.require(len(first["waits"]) == 1, "source/remap change did not occur during a delayed response")
                    low = first["waits"][0][1]
                    high = next(a for a, b, x, y in first["exchanges"] if a > low and x[0] == PULL)
                    if variant == 0:
                        q30.require(any(low < s < high and c != cpu and d[2] == first["request"]["page"] for s, c, d in generations),
                                    "delayed reply lacked actual peer source-generation change")
                    else:
                        q30.require(txs[1]["request"]["page"] != first["request"]["page"], "delayed reply lacked physical remap")
                    races = [(s, d) for s, kind, d in events if kind == CONTROL and d[0] == 10]
                    q30.require(len(races) == 1 and races[0][1][2] < races[0][1][3], "source-read-to-pin race was not forced")
                    q30.require(any(s > races[0][0] and d[1] == (3 if variant == 0 else 4) and d[2] == races[0][1][1] for s, d in rejects),
                                "source-read-to-pin race did not reject stale dispatch")
                    q30.require(objects[enters[0][1][0]]["tx"] is txs[-1], "rejected intermediate object later executed")
                else:
                    q30.require(first["flags"] == 4 and len(first["copied"]) == 32, "duplicate response did not follow first consumed chunk")
            elif group == 6:
                q30.require(len(txs) == 2 and len(enters) == len(effects) == 1 and txs[0]["complete"]
                            and not any(obj["tx"] is txs[0] for obj in objects.values()), "interrupted response was not invalidated and replaced")
                timing = result["apic_cases"][key]["events"][cpu]
                native_events = [(s, d) for s, k, d in timing if k == q32.NATIVE]
                dispatches = [(s, d) for s, k, d in timing if k == q32.DISPATCH and d[4] == 0]
                q30.require(len(native_events) == len(dispatches) == 3 and all(d[0] == variant + 1 for s, d in native_events),
                            "before/during/after source interrupt matrix missing")
                q30.require(native_events[0][0] < txs[0]["start"] and native_events[2][0] > effects[0][0],
                            "native interrupt did not bracket translation and generated completion")
                delayed = [(a, b, x, y) for a, b, x, y in txs[0]["exchanges"] if x[0] == PULL and y[10] == WAIT]
                q30.require(len(delayed) == 1 and delayed[0][0] < native_events[1][0] < delayed[0][1]
                            and native_events[1][1][4] == 7, "native arrival was not inside stopped-response copy interval")
                returns = [(s, d) for s, k, d in guest["events"][cpu] if k == q31.RETURN and s > native_events[1][0]]
                q30.require(returns, "delayed-response IRQ omitted actual guest IRET")
                changed = [s for s, c, d in generations if native_events[1][0] < s < returns[0][0]
                           and c == cpu and d[2] == txs[0]["request"]["page"]]
                q30.require(changed, "delayed IRQ handler did not invalidate outstanding source before IRET")
                if native:
                    delayed_source_witness(cpu, txs[0]["transaction"], variant, delayed[0][:2], native_events[1],
                                           events, timing, logs, symbols, timer_intid)
            elif group == 7:
                q30.require(len(txs) == 2 and txs[0]["flags"] == (4, 8, 16, 32, 64, 128)[variant]
                            and len(enters) == len(effects) == (2 if variant == 5 else 1), "malformed/partial transport continuation count")
                if variant < 5:
                    q30.require(txs[0]["cancelled"] and not any(obj["tx"] is txs[0] for obj in objects.values()), "malformed object executed")
                else:
                    observations = [(s, d) for row in scope["events"] for s, k, d in row if k == CONTROL and d[0] == 5 and d[1] == cpu]
                    first = txs[0]["chunks"][0]
                    q30.require(len(observations) == 1 and observations[0][1][2] == 32
                                and first["copy"] < observations[0][0] < first["ack"], "partial copy lacked peer progress before delayed consumption ACK")
                    q30.require(all(s > txs[0]["stop"] for s, d in enters), "partial native object became reachable early")
                q30.require(objects[enters[-1][1][0]]["tx"] is txs[-1], "invalid reply prevented valid fresh continuation")
            elif group == 8:
                q30.require(len(txs) == 2 and [tx["request"]["kind"] for tx in txs] == [1, 2] and len(enters) == len(effects) == 2,
                            "fetch-vs-miss and generated load/fault composition missing")
                denied = [s for s in steps if s["action"] == 7 and s["operation"]["end"][2]]
                q30.require(len(denied) == int(variant != 0), "guest fetch protection incorrectly became host production")
                if variant:
                    q30.require(denied[0]["operation"]["end"][2:4] == (14, 16 if variant == 1 else 17)
                                and denied[0]["sequence"] < txs[0]["start"], "absent/NX guest fetch error or request ordering")
                loads = [s for s in steps if s["action"] == 1 and enters[-1][0] < s["sequence"] < effects[-1][0]]
                q30.require(len(loads) == 2 and loads[0]["operation"]["end"][2:4] == (14, 0)
                            and loads[1]["operation"]["end"][1:3] == (0x123000 + cpu, 0), "actual generated operand fault/retry missing")
                if native:
                    q30.require(loads[0]["operation"]["sequence"] in native_faults, "generated fault did not use actual native exception path")
            elif group == 9:
                q30.require(len(txs) == 2 and [tx["request"]["kind"] for tx in txs] == [2 if variant else 3, 1]
                            and len(enters) == len(effects) == 2, "self/remote source write did not compose with old generated continuation")
                first = objects[enters[0][1][0]]
                changed = [(s, c, d) for s, c, d in generations if enters[0][0] < s < effects[0][0]
                           and d[2] == first["tx"]["request"]["page"] and (c != cpu if variant else c == cpu)]
                q30.require(changed, "source changed outside old generated pinned lifetime")
                if variant:
                    denied = [s for s in steps if s["action"] == 1 and s["operation"]["end"][2] == 14]
                    q30.require(len(denied) == 1 and changed[0][0] < denied[0]["sequence"] < effects[0][0],
                                "remote source write did not preserve old metadata through actual fault/retry")
                    if native:
                        q30.require(denied[0]["operation"]["sequence"] in native_faults,
                                    "old pinned generated load did not use actual native exception path")
                serial = [a for a in guest["attempts"][cpu] if a["instruction"] == 7 and effects[0][0] < a["sequence"] < enters[-1][0]]
                q30.require(len(serial) == 1 and any(serial[0]["sequence"] < s < enters[-1][0] and d[1] == 3 for s, d in rejects),
                            "source write did not serialize guest code and reject old dispatch")
            elif group == 10:
                if variant == 0:
                    q30.require(len(txs) == len(enters) == 1 and txs[0]["waits"] and txs[0]["complete"], "delayed host progress case missing")
                elif variant == 1:
                    limits = [(s, d) for s, k, d in events if k == CONTROL and d[0] == 6]
                    q30.require(len(txs) == 2 and len(limits) == len(enters) == 1 and txs[1]["cancelled"]
                                and objects[enters[0][1][0]]["tx"] is txs[0] and enters[0][0] > txs[1]["stop"],
                                "reduced allocator failure did not preserve an existing immutable object")
                else:
                    q30.require(len(txs) == 1 and txs[0]["cancelled"] and txs[0]["waits"] and not enters and not effects
                                and any(d[1] == 7 for s, d in rejects), "orderly termination did not cancel actual outstanding WAIT")
            covered += 1
    q30.require(partial or covered == 448 * ncpus, "incomplete Q33 finite CPU coverage")
    return covered


def add_retirement(source, before, after):
    expected = dict(before)
    expected["rax"], expected["rflags"] = add_semantics(source, before["rax"], before["rflags"])
    expected["rip"] += 6
    q30.require(after == expected, "generated ADD changed incorrect guest state or arithmetic flags")


def model_controls(header, records, operations, result, state, pool):
    rejected = []
    ncpus = header["ncpus"]
    def rejects(name, call):
        try: call()
        except ValueError: rejected.append(name)
        else: raise AssertionError("Q33 coherent corruption accepted: " + name)
    def finite_control(name, key, change, record_change=None):
        subset = dict(result, scopes={key: copy.deepcopy(result["scopes"][key])},
                      cases={key: copy.deepcopy(result["cases"][key])},
                      apic_cases={key: copy.deepcopy(result["apic_cases"][key])})
        scope = result["scopes"][key]
        own = [r for r in records if scope["start"] <= r[0] <= scope["stop"]]
        change(subset)
        if record_change: own = record_change(own)
        rejects(name, lambda: finite_semantics(subset, own, operations, ncpus, partial=True))
    finite_control("live replacement omitted one peer ACK", (3, 0, 0),
                   lambda r: r["scopes"][3, 0, 0]["events"][1].__setitem__(slice(None),
                       [e for e in r["scopes"][3, 0, 0]["events"][1] if not (e[1] == LINK and e[2][0] == 3)]))
    def early_link_ack(r):
        events = r["scopes"][3, 0, 0]["events"]
        replacement = max(s for row in events for s, k, d in row if k == LINK and d[0] == 1)
        row = events[1]
        for i, (s, k, d) in enumerate(row):
            if k == LINK and d[0] == 3: row[i] = (replacement + 1, k, d)
    finite_control("peer ACK before old generated call effect", (3, 0, 0), early_link_ack)
    finite_control("source-read-to-pin race witness missing", (5, 0, 0),
                   lambda r: r["scopes"][5, 0, 0]["events"][0].__setitem__(slice(None),
                       [e for e in r["scopes"][5, 0, 0]["events"][0] if not (e[1] == CONTROL and e[2][0] == 10)]))
    for variant in range(3):
        key = 6, variant, 0
        def remove_middle(r, key=key):
            row = r["apic_cases"][key]["events"][0]
            selected = [i for i, e in enumerate(row) if e[1] == q32.NATIVE][1]
            del row[selected]
        finite_control("missing during-host-delay source " + str(variant + 1), key, remove_middle)
    def late_observation(r):
        row = r["scopes"][7, 5, 0]["events"][1]
        for index, (sequence, kind, data) in enumerate(row):
            if kind == CONTROL and data[:2] == (5, 0): row[index] = (r["scopes"][7, 5, 0]["stop"] - 1, kind, data)
    finite_control("partial-copy peer progress only after consumption ACK", (7, 5, 0), late_observation)
    def wrong_fetch(r):
        step = next(s for s in r["cases"][8, 2, 0]["steps"][0] if s["action"] == 7 and s["operation"]["end"][2])
        end = list(step["operation"]["end"]); end[3] = 16; step["operation"]["end"] = tuple(end)
    finite_control("NX fetch misclassified as absent-page translation miss", (8, 2, 0), wrong_fetch)
    finite_control("remote source generation changed outside old active continuation", (9, 1, 0), lambda r: None,
                   lambda rows: [r for r in rows if not (r[1] == 13 and r[4][:2] == (q31.CODE, 2))])
    key = (9, 1, 0)
    native_subset = dict(result, scopes={key: result["scopes"][key]}, cases={key: result["cases"][key]},
                         apic_cases={key: result["apic_cases"][key]})
    native_rows = [r for r in records if result["scopes"][key]["start"] <= r[0] <= result["scopes"][key]["stop"]]
    for cpu, row in enumerate(result["cases"][key]["steps"]):
        op = next(s["operation"] for s in row if s["action"] == 1 and s["operation"]["end"][2] == 14)
        native_rows.append((op["sequence"] + 1, q30.FAULT, cpu, 0, (op["sequence"], 0, 0, 0, 0, 0, 0, 0)))
    # Only the shared finite native-witness predicate is exercised here;
    # synthetic fault records cannot pass the full paging/native-PC oracle.
    finite_semantics(native_subset, native_rows, operations, ncpus, native=True, partial=True)
    rejects("old pinned load lacks native exception witness", lambda: finite_semantics(native_subset,
            [r for r in native_rows if r[1] != q30.FAULT], operations, ncpus, native=True, partial=True))
    def missing_old(r):
        r["scopes"][10, 1, 0]["events"][0][:] = [e for e in r["scopes"][10, 1, 0]["events"][0] if e[1] != ENTER]
    finite_control("allocator exhaustion omitted existing-object continuation", (10, 1, 0), missing_old)
    key = 1, 0, 0
    case = result["cases"][key]
    attempt = next(a for a in case["attempts"][0] if a["instruction"] == 13)
    own = [s for s in case["states"][0] if s["attempt"] == attempt["identity"]]
    before = next(s["arch"] for s in own if s["point"] & 255 == 1)
    after = next(s["arch"] for s in own if s["point"] & 255 == 5)
    source = next(tx["request"]["source"] for tx in result["transactions"].values() if tx["scope"] == key and tx["cpu"] == 0)
    add_retirement(source, before, after)
    for name, field, xor in (("coherent wrong generated arithmetic", "rax", 1), ("coherent unused GPR corruption", "r10", 1),
                             ("coherent wrong arithmetic carry flag", "rflags", 1), ("wrong native retirement RIP", "rip", 1)):
        changed = dict(after); changed[field] ^= xor
        rejects(name, lambda: add_retirement(source, before, changed))
    def object_control(name, change):
        changed = list(records)
        change(changed)
        rejects(name, lambda: objects_semantics(result["transactions"], result["scopes"], result["cases"], changed,
                                                 ncpus, pool, state[2], header["symbols"]))
    def wrong_entry(rows):
        index = next(i for i, r in enumerate(rows) if r[1] == 15 and r[4][0] == ENTER)
        s, k, c, e, d = rows[index]; d = list(d); d[2] += 4; rows[index] = (s, k, c, e, tuple(d))
    object_control("actual generated entry PC points inside object", wrong_entry)
    def missing_publish(rows):
        index = next(i for i, r in enumerate(rows) if r[1] == 15 and r[4][0] == PUBLISH)
        del rows[index]
    object_control("native object entered without publication", missing_publish)
    key = 6, 0, 0
    altered = {key: copy.deepcopy(result["cases"][key])}
    step = next(s for s in altered[key]["steps"][0] if s["action"] == 4)
    begin = list(step["operation"]["begin"]); begin[3] ^= 8; step["operation"]["begin"] = tuple(begin)
    rejects("real interrupt frame word changed", lambda: q32.guest_semantics(altered, {key: result["apic_cases"][key]}, ncpus,
                                                                            additional_instructions=(5, 13, 16), nonretiring_probes=(5,)))
    altered = {key: copy.deepcopy(result["cases"][key])}
    step = next(s for s in altered[key]["steps"][0] if s["action"] == 2 and s["operation"]["begin"][0] == 0x1000C100)
    altered[key]["steps"][0].append(copy.deepcopy(step))
    rejects("duplicate coherent guest interrupt handler store", lambda: interrupt_effects(altered, {key: result["apic_cases"][key]}, ncpus))
    transfers = []
    for tx in result["transactions"].values():
        for before, after, request_words, response_words in tx["exchanges"]:
            item = dict(cpu=tx["cpu"], request=list(request_words), response=list(response_words), accepted=True)
            if request_words[0] == REQUEST_CHUNK and request_words[2] == 64: item["produced_hex"] = tx["produced"].hex()
            transfers.append((before, item))
    transfers = [item for before, item in sorted(transfers)]
    changed = list(records)
    index = next(i for i, r in enumerate(changed) if r[1] == 15 and r[4][:3] == (CONTROL, 11, 1))
    seq, kind, cpu, epoch, data = changed[index]; data = list(data); data[3] ^= 1
    changed[index] = (seq, kind, cpu, epoch, tuple(data))
    rejects("copied interrupted WAIT X4 corrupted while host response unchanged", lambda: transport(transfers, changed, ncpus))
    changed = list(records)
    index = next(i for i, r in enumerate(changed) if r[1] == 15 and r[4][0] == RECEIVE)
    seq, kind, cpu, epoch, data = changed[index]; data = list(data); data[4] ^= 1
    changed[index] = (seq, kind, cpu, epoch, tuple(data))
    rejects("actual received native bytes corrupted after host register setter", lambda: transport(transfers, changed, ncpus))
    return rejected


def final_state(header, cpus, blocks, result, native=True):
    q30.require(header["cases"] == 448 and header["phase"] == 2, "Q33 full finite execution did not finish")
    live = [b for b in blocks if b["id"]]
    q30.require(header["allocated"] == len(live) and header["pool_bytes"] == sum((b["length"] + 83) & ~63 for b in live),
                "immutable pool allocation counters disagree with actual descriptors")
    for cpu, state in enumerate(cpus):
        q30.require(state["started"] == state["done"] == 1 and state["cases"] == 448
                    and (state["group"], state["variant"], state["repetition"]) == (10, 2, 15)
                    and state["current"] == state["depth"] == state["transport_active"] == state["waiting"] == 0,
                    "Q33 final CPU ownership/completion state")
        q30.require(not native or state["timer_end"] > state["timer_start"], "Q33 native timer failed to progress")
        for name, value in result["totals"][cpu].items():
            q30.require(state[name] == value, "Q33 final raw/replay counter: " + name)
        fault_count = sum(d[1] == 14 for case in result["cases"].values() for s, k, d in case["events"][cpu] if k == q31.DELIVER)
        irq_count = sum(d[4] == 0 for case in result["apic_cases"].values() for s, k, d in case["events"][cpu] if k == q32.DISPATCH)
        q30.require(state["faults"] == fault_count and state["interrupts"] == irq_count
                    and state["restarts"] == state["source_generation"] == state["partial"] == 0,
                    "Q33 fault/IRQ counters or final ownership residual mismatch")
        q30.require(state["transaction"] == result["totals"][cpu]["requests"] and state["allocation_limit"] == OBJECTS,
                    "Q33 request identity or restored finite allocation limit")


def check(artifact, logs, symbols, ram, initial, version=5, terminal=True, snapshot_only=False):
    q30.require(version in (5,6) and (not snapshot_only or version==6 and not terminal), "explicit Q33 snapshot mode")
    symbols = dict(symbols, _text_end=min(symbols[k] for k in ("_l1", "_l2", "_device_l2", "_pages", "_aliases")))
    saved = q30.snapshot(artifact("q32-checkpoint.bin"), version)
    previous = q32.check(artifact, logs, symbols, saved, initial, version=version, terminal=False, snapshot_only=True)
    q30.require(previous["status"] == "PASS", "Q32 checkpoint lacks complete independent continuation acceptance")
    prefix, prefix_cpus, _, _ = q30.decode(saved, initial, version=version, records_required=False)
    old31, old31cpus, _, _, _ = q31.decode(saved, prefix["ncpus"], version=version)
    old32, old32cpus = q32.decode(saved, prefix["ncpus"])
    old33, old33cpus, oldblocks = decode(saved, prefix["ncpus"], version)
    header, paging_cpus, roles, records = q30.decode(ram, initial, version=version)
    ncpus, count = header["ncpus"], prefix["records"]
    qheader, cpus, blocks = decode(ram, ncpus, version)
    q30.require(ncpus == prefix["ncpus"] and header["records"] > count
                and (qheader["checkpoint_records"], qheader["checkpoint_epoch"]) == (count, prefix["epoch"]),
                "Q33 failed to continue exact accepted Q32 state")
    end = q30.LOG_OFFSET + count * 96
    q30.require(memoryview(saved)[q30.LOG_OFFSET:end] == memoryview(ram)[q30.LOG_OFFSET:end], "Q32 checkpoint log prefix changed")
    q30.require(old33 == dict(qheader, phase=0, cases=0, allocated=0, pool_bytes=0, pool_exhaustions=0)
                and all(not any(c.values()) for c in old33cpus) and all(not any(b.values()) for b in oldblocks)
                and not any(saved[POOL - 0x40000000:POOL_END - 0x40000000]), "Q33 objects or CPU execution predates retained Q32 checkpoint")
    summaries = [r for r in logs if "q32_checkpoint_dump_complete" in r]
    q30.require(len(summaries) == 1 and summaries[0]["q32_checkpoint_dump_complete"] is True
                and summaries[0]["q32_checkpoint_host_checks_pass"] is True
                and tuple(summaries[0]["q32_checkpoint_" + key] for key in ("ram_bytes", "records", "epoch")) ==
                (len(saved), count, prefix["epoch"]), "same-VM all-thread Q32 checkpoint missing")
    for cpu in range(ncpus):
        rows = [r for r in logs if r.get("cpu") == cpu and "q32_checkpoint_instruction_pc" in r]
        q30.require(len(rows) == 1, "Q32 checkpoint stopped-owner register evidence")
        row = rows[0]
        q30.require(row["q32_checkpoint_register_check"] is True and row["q32_checkpoint_actlr"] & 2
                    and row["q32_checkpoint_cpsr"] & 128 == 0
                    and row["q32_checkpoint_resume_pc"] == row["q32_checkpoint_instruction_pc"] + 4
                    and struct.unpack_from("<I", saved, row["q32_checkpoint_instruction_pc"] - 0x40000000)[0] == 0xD40006A2
                    and tuple(row["q32_checkpoint_" + key] for key in ("tcr", "ttbr0", "ttbr1")) ==
                    tuple(prefix_cpus[cpu]["final_" + key] for key in ("tcr", "ttbr0", "ttbr1")), "Q32 checkpoint native translation/register mismatch")
    del saved, oldblocks, old33cpus
    native_instructions(ram, symbols)
    code = CodeState()
    replay = q30.replay(initial, records, ncpus, version=version, code_state=code)
    q30.foundation(ram, paging_cpus, symbols, terminal=terminal, version=version)
    q30.validate_native_faults(records, symbols)
    q30.validate_snapshot(ram, header, paging_cpus, roles, replay)
    q30.require(snapshot_only or (artifact("q33.bin").read_bytes() == ram[STATE:STATE + STATE_BYTES]
                and artifact("q32.bin").read_bytes() == ram[q32.STATE:q32.STATE + q32.STATE_BYTES]
                and artifact("q31.bin").read_bytes() == ram[q31.STATE:q31.STATE + q31.STATE_BYTES]), "separate continued state dumps disagree")
    eligible = {(r["cpu"], r["request"][1]) for r in logs if r.get("q33_transfer") is True and (not snapshot_only or r["request"][12] <= 10)}
    produced = {}
    for row in logs:
        if row.get("q33_produced") is True and (not snapshot_only or (row["cpu"], row["transaction"]) in eligible):
            key = row["cpu"], row["transaction"]
            q30.require(key not in produced, "duplicate host compiler object identity")
            produced[key] = row
    transfer_rows = []
    consumed_production = set()
    for row in logs:
        if row.get("q33_transfer") is not True or (row["cpu"], row["request"][1]) not in eligible:
            continue
        row = dict(row)
        if row["request"][0] == REQUEST_CHUNK and row["request"][2] == 64:
            key = row["cpu"], row["request"][1]
            q30.require(key in produced and key not in consumed_production, "missing/repeated actual host object production")
            consumed_production.add(key)
            row.update(produced_hex=produced[key]["bytes_hex"], produced_request=produced[key]["request"])
        transfer_rows.append(row)
    q30.require(consumed_production == set(produced), "orphan host-produced object")
    settings = dict(timer_intid=q30.word(ram, 0x800000 + 32), spi_base=old32["spi_base"], symbols=symbols)
    result = semantic_trace(records, replay["operations"], ncpus, settings, transfer_rows,
                            ram[POOL - 0x40000000:POOL_END - 0x40000000], blocks, count, old31cpus, old32cpus, native=True)
    final_state(qheader, cpus, blocks, result)
    coverage = finite_semantics(result, records, replay["operations"], ncpus, native=True, logs=logs, symbols=symbols, timer_intid=settings["timer_intid"])
    q30.require(not replay["incomplete"], "unsupported continued native paging replay: " + repr(replay["incomplete"]))
    new31, new31cpus, generations, users, code_roles = q31.decode(ram, ncpus, version=version)
    new32, new32cpus = q32.decode(ram, ncpus)
    q30.require(new31 == old31 and new32 == old32 and tuple(code.generations) == generations and tuple(code.users) == users
                and bytes(code.roles) == code_roles and not any(users) and not any(code_roles)
                and not code.pending_generation and not any(code.pins.values()), "continued code/interrupt identity or code lifetime mismatch")
    for cpu in range(ncpus):
        actual, apic = new32cpus[cpu], result["apic"][cpu]
        q30.require(all(actual[name] == old32cpus[cpu][name] for name in
                    ("started", "done", "cases", "timer_start", "timer_end", "diagnostic_request", "diagnostic_ack", "diagnostic_case")),
                    "continued Q32 checkpoint completion identity changed")
        for name in ("native_count", "guest_count", "boundaries", "wake_generation"):
            q30.require(actual[name] == apic[name], "continued APIC counter mismatch: " + name)
        q30.require(actual["handler_effects"] == actual["guest_count"] and not q32.bitmap(actual["irr"])
                    and not q32.bitmap(actual["isr"]) and actual["shadow"] == actual["tpr"] == actual["halted"] == 0,
                    "continued IRQ left incomplete guest service")
        for name in ("emitted", "accepted", "vectors"):
            for source in range(3):
                for sender in range(4):
                    q30.require(actual[name][source][sender] == apic[name].get((source + 1, sender), 0), "continued native payload mailbox mismatch")
        q30.require(actual["emitted"] == actual["accepted"] and actual["sgi_sent"] == tuple(apic["sgi_sent"])
                    and actual["raw_irq"][1:] == tuple(apic["raw_irq"][1:])
                    and actual["empty_irq"][1:] == tuple(apic["empty_irq"][1:]), "continued notification ownership was not discharged")
        spi = [r for r in logs if r.get("cpu") == cpu and "q32_spi_action" in r]
        q30.require([(r["q32_spi_action"], r["intid"], r["sequence"]) for r in spi] ==
                    [(action, old32["spi_base"] + cpu, sequence) for sequence in range(1, actual["spi_request"] + 1)
                     for action in ("assert", "deassert")], "continued host SPI physical transitions mismatch")
        q30.require(actual["spi_request"] == actual["spi_sent"] == actual["spi_deassert"] == actual["spi_deasserted"]
                    == actual["emitted"][2][cpu] and actual["spi_level"] == 0, "continued SPI remained asserted/unacknowledged")
        state = new31cpus[cpu]
        for name in ("attempts", "commits", "deliveries", "snapshots"):
            q30.require(state[name] == result["counts"][cpu][name], "continued canonical-state metadata count")
        q30.require(state["code_writes"] == code.writes[cpu] and state["depth"] == 0
                    and state["arch"] == result["cases"][10, 2, 15]["states"][cpu][-1]["arch"]
                    and all(state[name] == old31cpus[cpu][name] for name in ("started", "done", "cases", "timer_start", "timer_end")),
                    "final canonical guest state or prior foundation counters changed")
        final_case = result["cases"][10, 2, 15]
        attempt = final_case["attempts"][cpu][-1]
        metadata = dict(identity=attempt["rip"], generation=attempt["generation"], rip=attempt["rip"],
                        length=attempt["length"], instruction=attempt["instruction"], access=0, width=attempt["width"], substep=0)
        later_steps = [s for s in final_case["steps"][cpu] if s["sequence"] > attempt["sequence"]]
        if later_steps:
            recipe = later_steps[-1]["recipe"]
            metadata.update(identity=recipe["rip"], generation=recipe["generation"], rip=recipe["rip"],
                            instruction=recipe["instruction"], access=recipe["access"], width=recipe["width"], substep=recipe["substep"])
        q30.require(state["current_attempt"] == attempt["identity"] and state["metadata"] == metadata,
                    "final continued native recovery metadata differs from actual last recipe")
        reports = [r for r in logs if r.get("cpu") == cpu and "q33_started" in r]
        q30.require(snapshot_only or len(reports) == 1 and all(reports[0]["q33_" + name] == cpus[cpu][name] for name in
                    ("started", "done", "cases", "requests", "chunks", "entries", "effects", "timer_start", "timer_end")),
                    "host/raw Q33 CPU completion mismatch")
    return dict(format="natemu-q33-independent-check-v1", status="PASS", checkpoint=previous,
                records=len(records), operations=len(replay["operations"]), cases=len(result["cases"]), cpu_cases=coverage,
                checked=["unchanged accepted Q30/Q31/Q32 checkpoints", "full continuing paging/code-generation replay",
                         "actual stopped-register native objects and guest canonical effects",
                         "all ten finite groups, native IRQ register preservation, publication and ownership"], unsupported=[])



def self_check():
    rejected = []
    def rejects(name, function):
        try:
            function()
        except ValueError:
            rejected.append(name)
        else:
            raise AssertionError("Q33 corruption accepted: " + name)
    words = [PULL, 1, 0, 32, 0x1122334455667788, 0, 0, 0, 64, 1, OK, 0, 1, 0, 0, 0]
    assert packet(words, 0)["payload"][:8] == bytes.fromhex("8877665544332211")
    for name, index, value in (("cross-vCPU response", 15, 1), ("oversized response chunk", 3, 33),
                               ("wrapped transfer offset", 2, q30.MASK), ("unknown transfer status", 10, 4)):
        changed = list(words); changed[index] = value
        rejects(name, lambda: packet(changed, 0))
    assert add_semantics(0x00000548 | (1 << 16), q30.MASK, 0x202) == (0, 0x257)
    assert add_semantics(0x00000548 | (1 << 16), 0x7FFFFFFFFFFFFFFF, 0x202) == (0x8000000000000000, 0xA96)
    rejects("unsupported guest source opcode", lambda: add_semantics(0, 0, 0))
    code_words = [0xA9BE7BFD, 0xA90153F3, 0x910003FD, 0xAA0003F3, 0xAA0103F4,
                  0xAA1403E0, 0x10FFFF41, 0xAA1303E2, 0xF9402A90, 0xD63F0200, 0xAA0003F3]
    code_words += [0xD503201F] * 4
    code_words += [0x91001E73, 0xAA1403E0, 0x10FFFDE1, 0xAA1303E2, 0xF9402E90,
                   0xD63F0200, 0xA94153F3, 0xA8C27BFD, 0xD65F03C0]
    code = struct.pack("<24I", *code_words)
    decode_template(code, POOL, 1, 0x70548)
    for name, index, value in (("wrong native ADD immediate", 15, 0x91002273),
                               ("callback load wrong descriptor field", 8, 0xF9402690),
                               ("callback ADR wrong actual entry", 6, 0x10000001),
                               ("native result bypasses arithmetic", 18, 0xAA1F03E2),
                               ("native return loses saved registers", 21, 0xD503201F)):
        changed = list(code_words); changed[index] = value
        rejects(name, lambda: decode_template(struct.pack("<24I", *changed), POOL, 1, 0x70548))
    transfers, trace = [], []
    def emit(subtype, *payload):
        trace.append((len(trace) + 1, 15, 0, 0, (subtype, *payload, *([0] * (7 - len(payload))))))
    def exchange(op, offset=0, length=0, total=96, payload=b"", produced=None, status=OK):
        registers = [op, 1, offset, length, *struct.unpack("<4Q", payload.ljust(32, b"\0")), total, 1, 0, 0, 1, 0, 0, 0]
        reply = list(registers); reply[10] = status
        if op == PULL:
            reply[4:8] = struct.unpack("<4Q", code[offset:offset + length].ljust(32, b"\0"))
        emit(CONTROL, 1, op, 1, offset, 0, 0, 0)
        item = dict(cpu=0, request=registers, response=reply, accepted=True)
        if produced is not None: item["produced_hex"] = produced.hex()
        transfers.append(item)
        emit(CONTROL, 2, op, 1, offset, status, 0, 0)
        return reply
    recipe = (0x10003000, 0x300000, 0x303000, 1, 0x70548, 7, 1, 96, 1, 0, 1, 17)
    emit(REQUEST, 1, recipe[0], recipe[2], 1, recipe[4], 1, 96)
    exchange(BEGIN)
    encoded = struct.pack("<12Q", *recipe)
    for offset in range(0, 96, 32):
        payload = encoded[offset:offset + 32]
        emit(SEND, 1, offset, 32, *struct.unpack("<4Q", payload))
        exchange(REQUEST_CHUNK, offset, 32, payload=payload, produced=code if offset == 64 else None)
    for offset in range(0, 96, 32):
        response = exchange(PULL, offset, 32)
        emit(RECEIVE, 1, offset, 32, *response[4:8])
        emit(CONTROL, 3, 1, POOL + 20 + offset, 32, offset + 32, 1, 0)
        exchange(CONSUMED, offset, 32, status=DONE if offset == 64 else OK)
        emit(ACK, 1, offset, 32, DONE if offset == 64 else OK, 96, 1, 0)
    checked = transport(transfers, trace, 2)
    assert bytes(checked[0, 1]["copied"]) == code
    changed = copy.deepcopy(transfers); changed[4]["response"][4] ^= 1
    rejects("register bytes differ from host-produced object", lambda: transport(changed, trace, 2))
    changed = copy.deepcopy(transfers); changed[3].pop("produced_hex")
    rejects("partial object omits actual host-produced bytes", lambda: transport(changed, trace, 2))
    changed = [r for r in trace if not (r[4][:2] == (CONTROL, 3) and r[4][5] == 32)]
    rejects("ACK before actual guest copy", lambda: transport(transfers, changed, 2))
    changed = copy.deepcopy(transfers); changed[1]["request"][4] ^= 8
    rejects("whole request differs from exported source snapshot", lambda: transport(changed, trace, 2))
    changed = copy.deepcopy(transfers); changed[-1]["response"][10] = OK
    rejects("final chunk omitted ownership completion", lambda: transport(changed, trace, 2))
    native_tables = bytearray(65536)
    geometry = {"_l1": 0x40001000, "_l2": 0x40002000, "_aliases": 0x40004000,
                "_device_l2": 0x40005000, "_pages": 0x40006000}
    def put(address, value): struct.pack_into("<Q", native_tables, address - 0x40000000, value)
    for index, address in ((0, geometry["_device_l2"]), (1, geometry["_l2"]), (2, geometry["_l2"] + 4096)):
        put(geometry["_l1"] + index * 8, address | 3)
    for index in range(800):
        put(geometry["_l2"] + index * 8, (geometry["_pages"] + index * 4096) | 3 if index < 8
            else (0x40000000 + index * 0x200000) | 0x701)
    put(geometry["_l2"] + 800 * 8, geometry["_aliases"] | 3)
    put(geometry["_aliases"], 0x40900703)
    for index in range(4096): put(geometry["_pages"] + index * 8, (0x40000000 + index * 4096) | 0x703)
    put(geometry["_device_l2"] + 128 * 8, 0x10000000 | 0x405 | (3 << 53))
    for index in range(8): put(geometry["_device_l2"] + (136 + index) * 8, (0x11000000 + index * 0x200000) | 0x405 | (3 << 53))
    q30.foundation_tables(native_tables, geometry, 5)
    for name, address, value in (("missing second native L1 bank", geometry["_l1"] + 16, 0),
                                  ("identity block bank transition wrong GPA", geometry["_l2"] + 512 * 8, 0x40200701),
                                  ("native alias after1600MiB missing", geometry["_l2"] + 800 * 8, 0),
                                  ("extra native identity beyond RAM", geometry["_l2"] + 801 * 8, 0xA4200701),
                                  ("generated-code4KiB identity permission changed", geometry["_pages"] + 1024 * 8, 0x40400783)):
        changed = bytearray(native_tables); struct.pack_into("<Q", changed, address - 0x40000000, value)
        rejects(name, lambda: q30.foundation_tables(changed, geometry, 5))
    memory = bytearray(q30.RAM_SIZE)
    for table, child, shift in ((0x200000, 0x201000, 39), (0x201000, 0x202000, 30), (0x202000, 0x203000, 21)):
        q30.put(memory, table + ((q30.VA >> shift) & 511) * 8, child | 7)
    q30.put(memory, 0x203000 + ((q30.VA >> 12) & 511) * 8, q30.A | 7)
    memory[q30.A:q30.A + 3] = bytes.fromhex("488b07")
    state = CodeState(); state.bind(memory, [q30.DEFAULT_CONTEXT] * 2); state.q33_started = True
    state.roles[q30.A // 4096] = 1; state.generations[q30.A // 4096] = 1
    state.event(1, 13, 0, 0, (q31.CODE, 3, q30.A, 0, 1, 0, 1, 1), 0, None, set())
    state.mutation(2, 0, q30.A, q30.CODE, 100, 1, 0, set())
    memory[q30.A:q30.A + 6] = bytes.fromhex("480507000000")
    state.event(3, 13, 0, 1, (q31.CODE, 2, q30.A, 1, 2, 100, 2, 1), 1, 0, set())
    old_load = (q31.ATTEMPT, 1, q30.VA, q30.VA, 1, 3, 8, 1)
    state.event(4, 13, 0, 1, old_load, 0, None, set())
    wrong = list(old_load); wrong[4] = 2
    rejects("pinned old generation forged instruction encoding", lambda: state.event(5, 13, 0, 1, tuple(wrong), 0, None, set()))
    wrong = list(old_load); wrong[7] = 2
    rejects("new generation claimed for old pinned instruction", lambda: state.event(6, 13, 0, 1, tuple(wrong), 0, None, set()))
    state.event(7, 13, 0, 1, (q31.CODE, 4, q30.A, 1, 0, 0, 2, 0), 0, None, set())
    rejects("expired old pin reused after release", lambda: state.event(8, 13, 0, 1, old_load, 0, None, set()))
    state.event(9, 13, 0, 1, (q31.CODE, 3, q30.A, 0, 1, 0, 2, 1), 0, None, set())
    state.event(10, 13, 0, 1, (q31.ATTEMPT, 2, q30.VA, q30.VA, 13, 6, 8, 2), 0, None, set())
    machine = bytearray(4096)
    sites = {"_q33_transfer": 0x40000000, "_q33_transfer_exit": 0x4000002C, "_q33_transfer_resume": 0x40000030,
             "_q33_transfer_copy_end": 0x40000098}
    words = [0xA9BE53F3, 0xF9000BFE, 0xAA0003F3]
    words += [0xA9400000 | (2 * i << 15) | ((2 * i + 1) << 10) | (19 << 5) | (2 * i) for i in range(8)]
    words += [0xD40006C2, 0xD50342FF, 0xD5033FDF] + [0xD503201F] * 16
    words += [0xA9000000 | (2 * i << 15) | ((2 * i + 1) << 10) | (19 << 5) | (2 * i) for i in range(8)]
    words += [0xF9400BFE, 0xA8C253F3, 0xD65F03C0]
    struct.pack_into(f"<{len(words)}I", machine, 0, *words)
    for index, (name, instruction) in enumerate(zip(("dc", "dsb1", "ic", "dsb2", "isb"),
                                                    (0xD50B7B20, 0xD5033B9F, 0xD50B7521, 0xD5033B9F, 0xD5033FDF))):
        sites["_q33_publish_" + name] = 0x40000200 + 4 * index
        struct.pack_into("<I", machine, 0x200 + 4 * index, instruction)
    native_instructions(machine, sites)
    for name, offset in (("response register pair omitted", 0x30 + 18 * 4), ("publisher first barrier omitted", 0x204),
                         ("publisher ISB omitted", 0x210), ("helper interrupt unmask omitted", 0x30)):
        changed = bytearray(machine); struct.pack_into("<I", changed, offset, 0xD503201F)
        rejects(name, lambda: native_instructions(changed, sites))
    witness = [dict(q33_stopped_source=stage, cpu=0, transaction=1, source=1, intid=27, pending=bool(index), active=False,
                    pending_word=(1 << 27) if index else 0, active_word=0, cpsr=0x85, timer_ctl=1 if not index else 5, timer_cval=100)
               for index, stage in enumerate(("before", "after"))]
    irq = (1, 27, sites["_q33_transfer_resume"] + 4, 5, 7, 1, 0)
    arm = (8, 1, 100, 90, 1, 0, 0)
    stopped_source_witness(0, 1, 0, irq, witness, sites, arm, 27)
    for name, changed in (("notification already pending before stopped delay", [dict(witness[0], pending=True, pending_word=1 << 27), witness[1]]),
                          ("active interrupt claimed as pending unserviced source", [witness[0], dict(witness[1], active=True, active_word=1 << 27)])):
        rejects(name, lambda: stopped_source_witness(0, 1, 0, irq, changed, sites, arm, 27))
    changed = list(irq); changed[2] = sites["_q33_transfer_copy_end"]
    rejects("IRQ after response-copy completion", lambda: stopped_source_witness(0, 1, 0, changed, witness, sites, arm, 27))
    changed = list(irq); changed[2] = sites["_q33_transfer_resume"] + 76
    rejects("IRQ after first response STP despite matching completed packet", lambda: stopped_source_witness(0, 1, 0, changed, witness, sites, arm, 27))
    unlatched = [witness[0], dict(witness[1], pending=False, pending_word=0)]
    stopped_source_witness(0, 1, 0, irq, unlatched, sites, arm, 27)
    for name, index, value in (("timer expired before owner stopped", 0, 5),
                               ("timer disabled before delay", 0, 0), ("timer masked before delay", 0, 3),
                               ("timer never expired", 1, 1), ("timer disabled after delay", 1, 4),
                               ("expired timer remains masked", 1, 7)):
        changed = copy.deepcopy(unlatched); changed[index]["timer_ctl"] = value
        rejects(name, lambda: stopped_source_witness(0, 1, 0, irq, changed, sites, arm, 27))
    changed = copy.deepcopy(unlatched); changed[1]["timer_cval"] += 1
    rejects("timer comparator changed while stopped", lambda: stopped_source_witness(0, 1, 0, irq, changed, sites, arm, 27))
    changed = tuple(v + 1 if i == 2 else v for i, v in enumerate(arm))
    rejects("timer comparator differs from guest arm", lambda: stopped_source_witness(0, 1, 0, irq, unlatched, sites, changed, 27))
    rejects("timer guest arm missing", lambda: stopped_source_witness(0, 1, 0, irq, unlatched, sites, None, 27))
    changed_irq = tuple(29 if i == 1 else v for i, v in enumerate(irq))
    changed = [dict(r, intid=29) for r in unlatched]
    rejects("coherently wrong timer INTID", lambda: stopped_source_witness(0, 1, 0, changed_irq, changed, sites, arm, 27))
    for variant, intid in ((1, 2), (2, 40)):
        source_irq = (variant + 1, intid, irq[2], irq[3], 7, 1, 0)
        rows = [dict(r, source=variant + 1, intid=intid, pending=bool(i), pending_word=(1 << (intid % 32)) if i else 0)
                for i, r in enumerate(witness)]
        stopped_source_witness(0, 1, variant, source_irq, rows, sites)
        rows[1] = dict(rows[1], pending=False, pending_word=0)
        rejects("SGI/SPI pending transition missing " + str(variant),
                lambda: stopped_source_witness(0, 1, variant, source_irq, rows, sites))
    response = [PULL, 1, 0, 32, 0, 0, 0, 0, 80, 9, WAIT, 1, 6, 0, 0, 0]
    audit = [(index + 1, (11, index, *response[index * 4:index * 4 + 4], 1)) for index in range(4)]
    copied_response(audit, response, 1)
    changed = list(audit); data = list(changed[1][1]); data[2] ^= 1; changed[1] = (2, tuple(data))
    rejects("copied WAIT payload differs although no bytes consumed", lambda: copied_response(changed, response, 1))
    print(json.dumps(dict(status="SYNTHETIC PRIMITIVES PASS", rejected=rejected,
                         unsupported=["complete transport protocol", "finite case coverage", "native execution and raw acceptance"], vm=False)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, nargs="?")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--model-trace", type=Path)
    parser.add_argument("--model-host", type=Path)
    parser.add_argument("--model-pool", type=Path)
    parser.add_argument("--model-state", type=Path)
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if args.model_trace:
        q30.require(args.model_host is not None and args.model_pool is not None, "model requires actual host packets and copied pool")
        header, records, operations, result, state = model_trace(args.model_trace, args.model_host, args.model_pool, args.model_state)
        rejected = model_controls(header, records, operations, result, state, args.model_pool.read_bytes())
        print(json.dumps(dict(status="SYNTHETIC SEMANTICS PASS", cases=len(result["cases"]), cpu_cases=result["coverage"], rejected=rejected, operations=len(operations),
                              transactions=len(result["transactions"]), objects=len(result["objects"]),
                              unsupported=["native protocol and raw envelope"], vm=False)))
        return
    if args.run:
        print(json.dumps(q30.check_run(args.run, continuation=check, version=5), indent=2, sort_keys=True))
        return
    parser.error("provide a retained run or explicit synthetic control mode")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Independent raw Q30 trace checker. Stdlib only; never executes a VM.

The record counter orders allocation, not publication or native instructions.
BEGIN/END bound an operation; their epoch fields are observations, not commits.
PASS requires the enumerated finite cases, complete raw replay, independent
foundation/provenance checks and observed contention. Unsupported overlapping
native effects return INCOMPLETE. Neither status accepts a research conclusion.
"""
import argparse
import bisect
import copy
import hashlib
import json
from pathlib import Path
import resource
import mmap
import struct

from x86_walk import walk, RAM_SIZE
from check_foundation import symbols, FIELDS as FOUNDATION_FIELDS

MASK = (1 << 64) - 1
RAM_BYTES, STATE, X86_OFFSET = 224 << 20, 16 << 20, 10 << 20
MAGIC, CAPACITY, CPU_WORDS = 0x5133305041474531, 2097152, 153
RAM_BYTES_V3, CAPACITY_V3 = 512 << 20, 5242880
RAM_BYTES_V4, CAPACITY_V4, ALIAS_V4 = 832 << 20, 8388608, 0x74000000
RAM_BYTES_V5, CAPACITY_V5, ALIAS_V5 = 1600 << 20, 16777216, 0xA4000000
RAM_BYTES_V6, CAPACITY_V6, ALIAS_V6 = 1984 << 20, 20971520, 0xBC000000
ROLES_OFFSET = STATE + 13 * 8 + 4 * CPU_WORDS * 8
LOG_OFFSET = ROLES_OFFSET + 1024
HEADER = "magic version ncpus failure records epoch gate owner lock barrier_arrived barrier_generation case_id completed_groups".split()
CPU = "started done active ack waiting shadow_used operations faults table_traps guest_faults epochs acknowledgements timer_start timer_end final_tcr final_ttbr0 final_ttbr1".split()
CONTEXT = "cr3 cpl wp nxe pge".split()
REQUEST = "operation gva kind width value compare rax flags status error gpa faults inflight".split()
CONTEXT_EVENT, BEGIN, END, MUTATION, GATE, ACK, ROLE, MAP, CASE, FAULT, WAIT, MARK = range(1, 13)
GUEST, AD, SETUP, CODE = 1, 2, 3, 4
ACCESS = ("read", "write", "write", "execute", "execute")
STATUS = {"ok": 0, "gp": 13, "pf": 14, "unsupported_ram": 256, "unsupported_attributes": 257}
DEFAULT_CONTEXT = (0x200000, 0, 1, 1, 0)
VA, HIGH, ALIAS, BIG = 0x123456789000, 0xFFFF923456789000, 0x234560000000, 0x345600000000
SPARE, ALT, A, B = 0x240000, ALIAS + 0x800000, 0x10000, 0x14000
VARIANTS = {1: 5, 2: 22, 3: 4, 4: 3, 5: 1, 6: 3, 7: 2, 8: 5, 9: 2, 10: 2, 11: 2, 12: 3}


def contended_entry(variant, cpu):
    if variant == 2:
        return 0x202000 + ((VA >> 21) & 511) * 8, SPARE, SPARE + 4096
    if variant == 3:
        return 0x200000 + ((VA >> 39) & 511) * 8, 0x201000, 0x221000
    return 0x203000 + ((VA >> 12) & 511) * 8 + (cpu * 8 if variant == 1 else 0), A, A if variant == 4 else B


def initial_fixture():
    memory = bytearray(RAM_SIZE)
    for page in range(0, RAM_SIZE, 4096):
        for offset in (0, 4088):
            struct.pack_into("<Q", memory, page + offset, 0x5A00000000000000 | (page + offset))
    memory[0x200000:0x250000] = bytes(0x50000)
    for address, result in ((0x4000, 42), (0x5000, 43)):
        struct.pack_into("<II", memory, address, 0xD2800000 | (result << 5), 0xD65F03C0)
        struct.pack_into("<II", memory, address + 4088, 0xD2800000 | (result << 5), 0xD65F03C0)
    return memory


def require(condition, message):
    if not condition:
        raise ValueError(message)


def word(memory, address, width=8):
    require(width in (1, 8) and 0 <= address <= len(memory) - width, "physical access bounds")
    return int.from_bytes(memory[address:address + width], "little")


def put(memory, address, value, width=8):
    require(width in (1, 8) and 0 <= address <= len(memory) - width, "physical mutation bounds")
    require(0 <= value < 1 << (8 * width), "mutation value exceeds width")
    memory[address:address + width] = value.to_bytes(width, "little")


def context_valid(context):
    cr3, cpl, wp, nxe, pge = context
    require(cr3 < RAM_SIZE and cr3 & 4095 == 0 and cpl in (0, 3)
            and wp in (0, 1) and nxe in (0, 1) and pge in (0, 1), "invalid Q30 context")


def snapshot(path, version):
    if version < 6:
        return path.read_bytes()
    with path.open("rb") as stream:
        return mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)


def file_digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dump_equal(path, ram, start, end, version):
    if path.stat().st_size != end - start:
        return False
    with path.open("rb") as stream:
        for offset in range(start, end, 1 << 20):
            length = min(1 << 20, end - offset)
            if stream.read(length) != ram[offset:offset + length]:
                return False
    return True


class RecordView:
    """Reiterable v6 trace; one decoded record per iteration, no tuple corpus."""
    def __init__(self, ram, offset, count):
        self.ram, self.offset, self.count = ram, offset, count
    def __len__(self):
        return self.count
    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, stride = index.indices(self.count)
            require(stride == 1, "unsupported trace slice stride")
            return RecordView(self.ram, self.offset + start * 96, max(0, stop-start))
        if index < 0:
            index += self.count
        if index < 0 or index >= self.count:
            raise IndexError(index)
        row = struct.unpack_from("<12Q", self.ram, self.offset + index * 96)
        return (*row[:4], row[4:])
    def __iter__(self):
        for index in range(self.count):
            yield self[index]


def decode(ram, initial, version=2, records_required=True):
    require(version in (2, 3, 4, 5, 6), "unsupported explicit Q30 envelope version")
    ram_bytes, capacity = {2: (RAM_BYTES, CAPACITY), 3: (RAM_BYTES_V3, CAPACITY_V3),
                           4: (RAM_BYTES_V4, CAPACITY_V4), 5: (RAM_BYTES_V5, CAPACITY_V5), 6: (RAM_BYTES_V6, CAPACITY_V6)}[version]
    require(len(ram) == ram_bytes and len(initial) == RAM_SIZE, "full RAM/initial snapshot sizes")
    header = dict(zip(HEADER, struct.unpack_from("<13Q", ram, STATE)))
    require(header["magic"] == MAGIC and header["version"] == version, "Q30 identity/version")
    require(header["ncpus"] in (2, 4), "Q30 CPU count")
    require(0 < header["records"] <= capacity, "Q30 trace empty/overflow")
    require(header["failure"] == header["gate"] == header["lock"] == header["barrier_arrived"] == 0,
            "Q30 failure or unfinished gate/lock/barrier")
    cpus = []
    for cpu in range(4):
        offset = STATE + 13 * 8 + cpu * CPU_WORDS * 8
        fields = struct.unpack_from(f"<{CPU_WORDS}Q", ram, offset)
        if cpu >= header["ncpus"]:
            require(not any(fields), "unused Q30 CPU slot")
            continue
        item = dict(zip(CPU, fields[:17]))
        item["context"] = fields[17:22]
        item["request"] = dict(zip(REQUEST, fields[22:35]))
        item["foundation"] = fields[35:]
        context_valid(item["context"])
        require(item["started"] == item["done"] == 1 and item["active"] == item["waiting"] == 0
                and item["request"]["inflight"] == 0, f"CPU {cpu} incomplete")
        require(2 <= item["shadow_used"] <= 128, f"CPU {cpu} shadow pool bounds")
        require(item["timer_end"] > item["timer_start"], f"CPU {cpu} no timer progress")
        cpus.append(item)
    if not records_required:
        require(version >= 4, "summary-only decoder reserved for explicitly revalidated Q32 checkpoints")
        return header, cpus, bytes(ram[ROLES_OFFSET:LOG_OFFSET]), []
    records = RecordView(ram, LOG_OFFSET, header["records"]) if version == 6 else []
    for index in range(header["records"]):
        sequence, kind, cpu, epoch, *data = struct.unpack_from("<12Q", ram, LOG_OFFSET + index * 96)
        require(sequence == index + 1, f"unpublished/noncontiguous record {index + 1}")
        require(1 <= kind <= version + 10 and cpu < header["ncpus"] and epoch <= header["epoch"],
                f"record {sequence} identity/context")
        if version != 6:
            records.append((sequence, kind, cpu, epoch, tuple(data)))
    require(not any(memoryview(ram)[LOG_OFFSET + header["records"] * 96:LOG_OFFSET + capacity * 96]),
            "published trace extends beyond record count")
    return header, cpus, bytes(ram[ROLES_OFFSET:LOG_OFFSET]), records


def table_roles(memory, contexts):
    """Reachability by (table, level, NXE), not by successful leaf walks.

    User/RW restrictions do not retire paging structures. Terminal/invalid
    entries cannot create a child reference. A page may have multiple levels.
    """
    roles, seen = bytearray(1024), set()
    pending = [(c[0], 4, c[3]) for c in contexts]
    while pending:
        address, level, nxe = pending.pop()
        key = address, level, nxe
        if key in seen or address > RAM_SIZE - 4096:
            continue
        seen.add(key)
        roles[address // 4096] |= 1 << (level - 1)
        if level == 1:
            continue
        for (entry,) in struct.iter_unpack("<Q", memory[address:address + 4096]):
            if not entry & 1 or entry & 0x80 or entry & (((1 << 52) - 1) ^ ((1 << 36) - 1)):
                continue
            if entry >> 63 and not nxe:
                continue
            pending.append((entry & ((1 << 36) - 4096), level - 1, nxe))
    return bytes(roles)


def operation_walk(memory, context, begin, address=None):
    gva, kind, width = begin[:3]
    if address is not None:
        gva = address
        width = 1
    if gva > MASK - width + 1 or (gva + width - 1) >> 47 not in (0, (1 << 17) - 1):
        return {"status": "gp", "error_code": 0, "gpa": None, "walked": [], "ad_required": []}
    return walk(memory, context[0], gva, ACCESS[kind], context[1], bool(context[2]), bool(context[3]))


def cmp_flags(a, b, previous):
    difference = (a - b) & MASK
    arithmetic = (int(a < b) | (4 if bin(difference & 255).count("1") % 2 == 0 else 0)
                  | (16 if (a & 15) < (b & 15) else 0) | (64 if not difference else 0)
                  | (128 if difference >> 63 else 0)
                  | (2048 if a >> 63 != b >> 63 and difference >> 63 != a >> 63 else 0))
    return (previous & ~0x8D5) | arithmetic


def replay(initial, records, ncpus, version=2, code_state=None):
    require(version in (2, 3, 4, 5, 6), "unsupported explicit replay version")
    memory = bytearray(initial)
    contexts = [DEFAULT_CONTEXT] * ncpus
    if code_state is not None:
        require(version >= 3, "code observer requires explicit v3/v4 replay")
        code_state.bind(memory, contexts)
    roles = bytearray(1024)
    phase, epoch, owner, acks = 0, 0, None, set()
    pending, completed, maps = {}, [], {}
    active, last_operation = {}, {}
    counts = [dict(operations=0, faults=0, table_traps=0, guest_faults=0, epochs=0,
                   acknowledgements=0) for _ in range(ncpus)]
    missing = set()
    roles_dirty = False
    last_epoch = [0] * ncpus
    exclusive_effect = False
    ad_effects, begins, leaves, previous_mutation = {}, {}, {}, 0
    serialized_writes = set()
    for sequence, kind, _, _, data in records:
        if kind == BEGIN:
            begins[sequence] = sequence
        elif kind == MARK and data[:2] == (6, 0):
            leaves[data[2]] = sequence
        elif kind == MUTATION:
            if data[4] == GUEST or version >= 3 and data[4] == CODE:
                serialized_writes.add(data[5])
            if data[4] == AD:
                lower = max(previous_mutation, leaves.get(data[5], begins.get(data[5], 0)))
                ad_effects.setdefault(data[0], []).append(dict(lower=lower, upper=sequence, old=data[1], new=data[2],
                                                             required_after=lower, required_before=sequence))
            previous_mutation = sequence
    ad_indices = {address: [effect["upper"] for effect in effects] for address, effects in ad_effects.items()}

    def sample(operation):
        expected = operation_walk(memory, operation["context"], operation["begin"])
        value = None
        if expected["status"] == "ok":
            width = operation["begin"][2]
            if (operation["begin"][0] & 4095) + width <= 4096:
                value = word(memory, expected["gpa"], width)
                operation["layouts"].add(tuple(range(expected["gpa"], expected["gpa"] + width)))
            else:
                last = operation_walk(memory, operation["context"], operation["begin"],
                                      operation["begin"][0] + width - 1)
                if last["status"] != "ok":
                    expected = last
                else:
                    first_bytes = 4096 - (operation["begin"][0] & 4095)
                    second_bytes = width - first_bytes
                    layout = tuple(range(expected["gpa"], expected["gpa"] + first_bytes))
                    layout += tuple(range(last["gpa"] - second_bytes + 1, last["gpa"] + 1))
                    operation["layouts"].add(layout)
                    value = int.from_bytes(bytes(memory[a] for a in layout), "little")
        candidate = (STATUS[expected["status"]], expected["error_code"] or 0,
                     expected["gpa"], value)
        operation["candidates"].add(candidate)
        return expected

    def sample_all():
        for operation in pending.values():
            sample(operation)

    for sequence, kind, cpu, stamp, data in records:
        require(1 <= kind <= version + 10, f"record {sequence}: unsupported replay event")
        require(kind != 15 or version >= 5 and getattr(code_state, "q33", False), "Q33 events require independent Q33 metadata replay")
        require(kind != 16 or version == 6 and getattr(code_state, "q34", False), "Q34 events require independent Q34 metadata replay")
        require(kind != 14 or getattr(code_state, "q32", False), "Q32 events require independent Q32 metadata replay")
        if code_state is not None:
            if code_state.event(sequence, kind, cpu, stamp, data, phase, owner, active):
                exclusive_effect = True
        elif kind >= 13:
            require(False, "Q31 events require integrated independent metadata/code state")
        require(stamp >= last_epoch[cpu], f"record {sequence}: CPU epoch regressed")
        last_epoch[cpu] = stamp
        if kind == GATE:
            new_phase, stated_owner = data[:2]
            require(stated_owner == cpu, f"record {sequence}: gate owner")
            if new_phase == 1:
                require(phase == 0 and stamp == epoch + 1 and cpu not in active, f"record {sequence}: nested/skipped epoch")
                phase, epoch, owner, acks = 1, stamp, cpu, set()
                exclusive_effect = False
                counts[cpu]["epochs"] += 1
                maps.clear()
            else:
                require(cpu == owner and stamp == epoch, f"record {sequence}: foreign gate")
                require((phase, new_phase) in ((1, 2), (2, 0)), f"record {sequence}: gate order")
                if new_phase == 2:
                    require(not active, f"record {sequence}: gate completed before native quiescence")
                if new_phase == 2 and roles_dirty:
                    require(bytes(roles) == table_roles(memory, contexts),
                            f"record {sequence}: premature/missing table retirement or tracking")
                    roles_dirty = False
                if new_phase == 0:
                    require(acks == set(range(ncpus)), f"record {sequence}: missing remote ACK")
                phase = new_phase
                if phase == 0:
                    sample_all()
        elif kind == ACK:
            require(phase == 2 and stamp == epoch and data[0] == epoch and cpu not in acks,
                    f"record {sequence}: early/duplicate/wrong-epoch ACK")
            require(cpu not in active, f"record {sequence}: ACK while native operation active")
            require(data[1] in range(5) and data[2] in (0, 1), f"record {sequence}: ACK state")
            require((cpu == owner) == (data[1] == 0), f"record {sequence}: owner/remote ACK path")
            if cpu != owner:
                operation = pending.get(cpu, last_operation.get(cpu))
                require(data[3] == int(cpu in pending) and data[4] == (operation["sequence"] if operation else 0)
                        and data[5] == (operation["faults"] if operation else 0),
                        f"record {sequence}: remote ACK request/fault context")
            acks.add(cpu)
            counts[cpu]["acknowledgements"] += 1
        elif kind == CONTEXT_EVENT:
            require(phase == 1 and owner == cpu and stamp == epoch and cpu not in pending and not active,
                    f"record {sequence}: context outside exclusive change")
            context_valid(data[:5])
            contexts[cpu] = data[:5]
            roles_dirty = True
            exclusive_effect = True
        elif kind == BEGIN:
            require(cpu not in pending, f"record {sequence}: nested native operation")
            require(data[1] in range(4) and (data[2] == 8 or data[1:3] == (1, 1)
                    or version >= 3 and data[1:3] == (0, 1))
                    or version >= 3 and data[1:3] == (4, 1),
                    f"record {sequence}: operation kind/width")
            require(data[1] != 2 or data[0] & 7 == 0, f"record {sequence}: unaligned atomic")
            previous = last_operation.get(cpu)
            require(data[6] == (previous["end"][6] if previous else 0x202)
                    and data[5] == (data[4] if data[1] == 2 else previous["end"][5] if previous else 0),
                    f"record {sequence}: unlogged RAX/flags change between operations")
            operation = dict(sequence=sequence, cpu=cpu, begin=data, context=contexts[cpu],
                             candidates=set(), layouts=set(), mutations=[], faults=0, maps=0, native_intervals=[])
            pending[cpu] = operation
            operation["initial_walk"] = sample(operation)
        elif kind == FAULT:
            require(cpu in pending and data[0] == pending[cpu]["sequence"],
                    f"record {sequence}: orphan fault")
            require(active.get(cpu) is pending[cpu], f"record {sequence}: fault outside native interval")
            begin = pending[cpu]["begin"]
            native_va = begin[0] if begin[0] >> 63 else begin[0] | (1 << 47)
            require(data[3] >> 26 in (0x21, 0x25) and
                    (4 <= data[3] & 63 <= 7 or 12 <= data[3] & 63 <= 15),
                    f"record {sequence}: native fault syndrome")
            require(native_va <= data[2] < native_va + begin[2], f"record {sequence}: native FAR")
            pending[cpu]["faults"] += 1
            counts[cpu]["faults"] += 1
        elif kind == MUTATION:
            address, old, new, width, cause, operation_id, mask, reserved = data
            require((cause in (GUEST, AD, SETUP) or version >= 3 and code_state is not None and cause == CODE)
                    and width in (1, 8) and
                    (width == 1 or address & 7 == 0) and reserved == 0,
                    f"record {sequence}: mutation format")
            if word(memory, address, width) != old:
                # BEGIN/END do not order a native store against another CPU's
                # writer-locked mutation. Do not turn that ambiguity into PASS.
                possible = [p for p in pending.values() if p["begin"][1] in (1, 2)
                            and any(c[2] is not None and c[2] <= address < c[2] + p["begin"][2]
                                    for c in p["candidates"])]
                require(possible, f"record {sequence}: mutation old bytes disagree with replay")
                missing.add("native write overlapping serialized mutation needs bounded replay")
                put(memory, address, old, width)
            if cause != AD:
                require(phase == 1 and owner == cpu and stamp == epoch and mask == 0,
                        f"record {sequence}: guest/setup write outside exclusive epoch")
                roles_dirty = True
                exclusive_effect = True
                require(not active, f"record {sequence}: table write before native quiescence")
            if cause in (GUEST, AD, CODE):
                require(cpu in pending and operation_id == pending[cpu]["sequence"],
                        f"record {sequence}: orphan guest/A-D mutation")
                operation = pending[cpu]
                expected = operation_walk(memory, contexts[cpu], operation["begin"])
                require(expected["status"] == "ok", f"record {sequence}: mutation after guest denial")
                if cause == AD:
                    required = {entry["entry_gpa"]: entry["set_mask"] for entry in expected["ad_required"]}
                    # The second page of an unaligned scalar has a second walk.
                    if (operation["begin"][0] & 4095) + operation["begin"][2] > 4096:
                        second = operation_walk(memory, contexts[cpu], operation["begin"],
                                                operation["begin"][0] + operation["begin"][2] - 1)
                        required.update({entry["entry_gpa"]: entry["set_mask"] for entry in second["ad_required"]})
                    require(width == 8 and required.get(address) == mask and new == old | mask
                            and new != old, f"record {sequence}: wrong A/D update")
                else:
                    begin = operation["begin"]
                    require(operation["faults"] > 0 and begin[1] in (1, 2)
                            and expected["gpa"] == address and begin[2] == width,
                            f"record {sequence}: wrong guest-write GPA/width/access")
                    tracked_code = code_state is not None and code_state.roles[address // 4096]
                    require(roles[address // 4096] != 0 if cause == GUEST else tracked_code,
                            f"record {sequence}: guest write to untracked page")
                    wanted = old if begin[1] == 2 and old != begin[4] else begin[3] & ((1 << (8 * width)) - 1)
                    require(new == wanted and not operation["mutations"], f"record {sequence}: guest store/RMW commit")
                    for entry in expected["ad_required"]:
                        require(word(memory, entry["entry_gpa"]) & entry["set_mask"] == entry["set_mask"],
                                f"record {sequence}: missing successful write A/D")
                    operation["mutations"].append((address, old, new, width))
                    if cause == GUEST:
                        counts[cpu]["table_traps"] += 1
            put(memory, address, new, width)
            if code_state is not None:
                code_state.mutation(sequence, cpu, address, cause, operation_id, phase, owner, active)
                if code_state.roles[address // 4096]:
                    exclusive_effect = True
            if phase == 0:
                sample_all()
        elif kind == ROLE:
            exclusive_effect = True
            require(not active, f"record {sequence}: role change before native quiescence")
            address, old, new = data[:3]
            require(phase == 1 and cpu == owner and stamp == epoch and address < RAM_SIZE
                    and address & 4095 == 0 and old == roles[address // 4096]
                    and new < 16 and new != old, f"record {sequence}: table-role transition")
            roles[address // 4096] = new
        elif kind == MAP:
            require(cpu in pending and cpu not in active, f"record {sequence}: orphan/active native mapping")
            gva, gpa, slot, descriptor, page_size, rights, writable, role = data
            operation = pending[cpu]
            require(operation["faults"] or (operation["begin"][0] & 4095) + operation["begin"][2] > 4096,
                    f"record {sequence}: map without native miss or cross-page preparation")
            expected = operation_walk(memory, contexts[cpu], operation["begin"], gva)
            require(expected["status"] == "ok", f"record {sequence}: map bypasses cumulative permission")
            require(gva & 4095 == gpa & 4095 == 0 and gpa == expected["gpa"] & ~4095
                    and page_size == expected["leaf_size"], f"record {sequence}: wrong mapped GPA/page size")
            r = expected["rights"]
            wanted_rights = 1 | (2 if r["rw"] else 0) | (4 if r["user"] else 0) | (8 if r["execute"] else 0)
            require(rights == wanted_rights, f"record {sequence}: omitted nonleaf permission")
            require(role == roles[gpa // 4096], f"record {sequence}: map role disagreement")
            leaf = expected["walked"][-1]["value"]
            wanted_write = int(r["write"] and bool(leaf & 64) and not role
                               and not (code_state is not None and code_state.roles[gpa // 4096]))
            wanted_descriptor = (0x40A00000 + gpa) | 0xF03 | (1 << 54)
            if not wanted_write:
                wanted_descriptor |= 128
            if not r["execute"]:
                wanted_descriptor |= 1 << 53
            require(writable == wanted_write and descriptor == wanted_descriptor,
                    f"record {sequence}: unsafe native permissions/alias escape")
            require(0x40200000 + cpu * 0x80000 <= slot < 0x40200000 + (cpu + 1) * 0x80000
                    and slot & 7 == 0 and (cpu, gva) not in maps, f"record {sequence}: shadow slot/replacement")
            for entry in expected["ad_required"]:
                require(word(memory, entry["entry_gpa"]) & entry["set_mask"] == entry["set_mask"],
                        f"record {sequence}: missing successful miss A/D")
            maps[cpu, gva] = slot, descriptor
            operation["maps"] += 1
        elif kind == END:
            require(cpu in pending and data[0] == pending[cpu]["sequence"], f"record {sequence}: orphan/duplicate END")
            operation = pending[cpu]
            operation["end_walk"] = sample(operation)
            operation.update(end=data, end_sequence=sequence)
            begin, candidates = operation["begin"], operation["candidates"]
            result, status, error, reported_gpa, rax, flags, faults = data[1:]
            require((cpu in active) == (status == 0), f"record {sequence}: completion/native interval mismatch")
            require(faults == operation["faults"], f"record {sequence}: fault count")
            require(any((c[0], c[1]) == (status, error) for c in candidates),
                    f"record {sequence}: stale/incorrect status or missing cumulative permission")
            if reported_gpa != MASK:
                require(any(c[0] == 0 and c[2] == reported_gpa for c in candidates),
                        f"record {sequence}: wrong resolved GPA")
            if version >= 5 and begin[1] == 4 and status == 0:
                require(reported_gpa != MASK and reported_gpa == operation["end_walk"]["gpa"],
                        f"record {sequence}: successful fetch omitted or misstated pinned physical GPA")
            if status:
                require(result == 0 and not operation["mutations"] and rax == begin[5] and flags == begin[6],
                        f"record {sequence}: fault committed instruction results")
                counts[cpu]["guest_faults"] += 1
            elif begin[1] in (0, 4):
                require(rax == begin[5] and flags == begin[6], f"record {sequence}: load changed instruction state")
                allowed = {c[3] for c in candidates if c[0] == 0}
                # A native table-data load can see an A/D store before that
                # writer allocates its MUTATION record. The preceding serialized
                # mutation record and the writer's fault-leave bound that store.
                # Bounds are narrowed across reads so new-then-old cannot pass.
                relevant_ad = []
                for address in {c[2] for c in candidates if c[0] == 0}:
                    effects = ad_effects.get(address, [])
                    start_index = bisect.bisect_right(ad_indices.get(address, []), operation["active_start"])
                    for effect in effects[start_index:]:
                        if effect["lower"] >= sequence:
                            break
                        if begin[2] == 8 and any(c[3] is not None and (c[3] ^ effect["old"]) & ~96 == 0 for c in candidates):
                            allowed.update((effect["old"], effect["new"]))
                            relevant_ad.append(effect)
                for peer in pending.values():
                    if peer is operation or peer["begin"][1] not in (1, 2):
                        continue
                    # A trapped write resumes past its native store/CAS. Its
                    # sole effect is the GUEST mutation, fully validated below
                    # or earlier in this replay, including quiescence and old/new
                    # bytes. Its requested value is not another native effect.
                    if peer["sequence"] in serialized_writes:
                        continue
                    if any(c[2] == p[2] and c[2] is not None for c in candidates for p in peer["candidates"]):
                        peer_value = peer["begin"][3] & ((1 << (8 * peer["begin"][2])) - 1)
                        if peer_value not in allowed:
                            missing.add("overlapping native data read/write needs bounded replay")
                            allowed.add(peer_value)
                if None not in allowed:
                    require(result in allowed, f"record {sequence}: stale post-ACK or wrong native load")
                for effect in relevant_ad:
                    if result == effect["new"]:
                        effect["required_before"] = min(effect["required_before"], sequence)
                    elif result == effect["old"]:
                        effect["required_after"] = max(effect["required_after"], operation["active_start"])
                    require(effect["required_after"] <= effect["required_before"],
                            f"record {sequence}: inconsistent concurrent A/D load order")
            elif begin[1] in (1, 2):
                if begin[1] == 1:
                    require(result == begin[3] and rax == begin[5] and flags == begin[6],
                            f"record {sequence}: store return/instruction state")
                if begin[1] == 2:
                    require(rax == (begin[4] if result == begin[4] else result)
                            and flags == cmp_flags(begin[4], result, begin[6]),
                            f"record {sequence}: CMPXCHG RAX/flags")
                    if operation["mutations"]:
                        require(result == operation["mutations"][0][1], f"record {sequence}: trapped CMPXCHG old value")
                if not operation["mutations"]:
                    layouts = operation["layouts"]
                    if len(layouts) != 1:
                        missing.add("native write spanning translation changes needs bounded replay")
                    else:
                        layout = next(iter(layouts))
                        require(all(not roles[address // 4096]
                                    and not (code_state is not None and code_state.roles[address // 4096])
                                    for address in layout),
                                f"record {sequence}: untrapped table alias write")
                        if begin[1] == 2:
                            require(any(c[3] == result for c in candidates if c[0] == 0),
                                    f"record {sequence}: CMPXCHG old value")
                        value = result if begin[1] == 2 and result != begin[4] else begin[3]
                        effect = {address: (value >> (8 * index)) & 255 for index, address in enumerate(layout)}
                        for peer in pending.values():
                            if peer is operation or peer["begin"][1] not in (1, 2) or peer["mutations"]:
                                continue
                            for peer_layout in peer["layouts"]:
                                for index, address in enumerate(peer_layout):
                                    if address in effect and effect[address] != (peer["begin"][3] >> (8 * index)) & 255:
                                        missing.add("overlapping unequal native stores need bounded replay")
                        for index, address in enumerate(layout):
                            memory[address] = (value >> (8 * index)) & 255
            else:
                allowed = set()
                for candidate in candidates:
                    if candidate[0] != 0 or candidate[3] is None:
                        continue
                    instruction, ret = candidate[3] & 0xFFFFFFFF, candidate[3] >> 32
                    require(instruction & 0xFFE0001F == 0xD2800000 and ret == 0xD65F03C0,
                            f"record {sequence}: execute target outside immutable MOVZ/RET fixture")
                    allowed.add((instruction >> 5) & 0xFFFF)
                require(result in allowed and rax == begin[5] and flags == begin[6],
                        f"record {sequence}: wrong native execute result")
            del pending[cpu]
            counts[cpu]["operations"] += 1
            completed.append(operation)
            last_operation[cpu] = operation
            sample_all()
        elif kind == WAIT:
            operation = pending.get(cpu, last_operation.get(cpu))
            require(data[0] == (operation["sequence"] if operation else 0)
                    and data[1] == int(cpu in pending) and data[2] == (operation["faults"] if operation else 0)
                    and cpu not in active,
                    f"record {sequence}: writer-wait operation context")
        elif kind == MARK and data[0] == 6:
            operation = pending.get(cpu, last_operation.get(cpu))
            require(operation is not None and operation["sequence"] == data[2] and data[1] in (0, 1),
                    f"record {sequence}: orphan active interval")
            if data[1]:
                require(cpu in pending and cpu not in active and phase != 2 and not (phase == 1 and exclusive_effect),
                        f"record {sequence}: native entry during exclusive mutation/ACK")
                active[cpu] = operation
                operation["active_start"] = sequence
                operation["candidates"].clear()
                operation["layouts"].clear()
                sample(operation)
            else:
                require(active.get(cpu) is operation, f"record {sequence}: unmatched native leave")
                operation["native_intervals"].append((operation["active_start"], sequence))
                del active[cpu]
        elif kind in (CASE, MARK):
            pass  # Decoding a label does not establish that its scenarios ran.
    require(not pending and not active and phase == 0, "unfinished operations/native interval/epoch")
    require(bytes(roles) == table_roles(memory, contexts), "final table retirement/reachability")
    return dict(memory=memory, contexts=contexts, roles=bytes(roles), counts=counts,
                epoch=epoch, owner=owner, maps=maps, operations=completed, incomplete=missing)


def case_operations(group, variant, cpu, ncpus):
    """Independent fixed input/outcome enumeration; table-update helpers omitted.

    Each tuple is (kind, linear address, status, error, required result or None).
    Walk, mutation, permissions, A/D, flags and physical effects are checked by
    replay; this list checks that the intended actual operations occurred.
    """
    seed = lambda address: 0x5A00000000000000 | address
    read = lambda address, result=None: (0, address, 0, 0, result)
    store = lambda address, value: (1, address, 0, 0, value)
    fault = lambda kind, address, error: (kind, address, 14, error, 0)
    execute = lambda address, value: (3, address, 0, 0, value)
    if group == 1:
        if variant:
            return [fault(0, VA, 0), fault(0, VA, 9)]
        return [read(VA, seed(A)), read(HIGH, seed(B)), read(0, seed(A)),
                read(0x7FFFFFFFF000, seed(B)), read(0xFFFF800000000000, seed(A)),
                read(0xFFFFFFFFFFFFF000, seed(B)), read(0x7FFFFFFFFFF8, seed(B + 4088)),
                read(0xFFFFFFFFFFFFFFF8, seed(B + 4088)), store(VA + cpu * 4096, 0xABC000 + cpu),
                read(ALIAS + A + cpu * 4096, 0xABC000 + cpu),
                (0, 0x800000000000, 13, 0, 0), (0, 0xFFFF7FFFFFFFF000, 13, 0, 0),
                (0, 0x7FFFFFFFFFFC, 13, 0, 0)]
    if group == 2:
        if variant < 16:
            mode = variant % 4
            cpl, wp = (3 if mode >= 2 else 0), mode & 1
            return [read(VA, seed(A)), fault(1, VA, 3 + (4 if cpl else 0)) if cpl or wp else store(VA, seed(A)),
                    fault(0, VA, 5) if cpl else read(VA, seed(A))]
        if variant < 20:
            return [execute(VA, 42), fault(3, VA, 17), fault(0, VA, 9)]
        return ([execute(HIGH, 43)] if variant == 20 else
                [execute(address, 42) for address in (0, 0x7FFFFFFFFFF8, 0xFFFF800000000000, 0xFFFFFFFFFFFFFFF8)])
    if group == 3:
        return [fault(0, VA, 0), read(VA, seed(A)), read(VA, seed(A)), fault(1, VA, 3),
                store(VA, seed(A)), fault(0, VA, 5), read(VA, seed(A)), fault(3, VA, 17), execute(VA, 42)]
    if group in (4, 12):
        if variant == 2:
            return [execute(VA, 42), execute(VA, 43), fault(3, VA, 17)]
        return ([read(VA, seed(A)), read(VA, seed(B)), read(VA, seed(B)), fault(1, VA, 3), fault(0, VA, 0)]
                if variant == 0 else [read(VA, seed(B))])
    if group == 5:
        return [fault(0, VA, 0), read(VA, seed(B))]
    if group == 6:
        return ([read(VA, seed(A))] if variant < 2 else []) + [store(VA, seed(A))]
    if group == 7:
        offset = ((VA >> 12) & 511) * 8
        if variant == 1:
            return ([(2, ALT + offset, 0, 0, None)] if cpu == 0 else []) + [fault(2, ALT + offset, 3)]
        expected = [store(ALIAS + SPARE, 0), store(ALT, 0)]
        for alias in (ALIAS + SPARE, ALT):
            for writer in range(ncpus):
                if cpu == writer:
                    expected += [store(alias + offset, B | 0x67), store(alias + offset, 0x67),
                                 (2, alias + offset, 0, 0, None), (2, alias + offset, 0, 0, None)]
                else:
                    expected += [read(VA)] * 16
                expected.append(read(VA, seed(A)))
        return expected
    if group == 8:
        entry, first, second = contended_entry(variant, cpu)
        expected = []
        for index in range(32):
            flags = 0x405 if variant == 4 and (index + cpu) & 1 else 0x407
            expected += [store(ALIAS + entry, (second if (index + cpu) & 1 else first) | flags),
                         read(VA + (cpu * 4096 if variant == 1 else 0)), read(ALIAS + entry)]
            if variant == 4:
                expected.append((1, VA, None, None, None))  # Concurrent permission: OK or PF(W|P).
            if index & 1:
                expected.append((2, ALIAS + entry, 0, 0, None))
        return expected
    if group == 9:
        first, last = (B, A) if cpu & 1 else (A, B)
        return [read(VA, seed(first)), read(VA, seed(first)), read(VA, seed(last))]
    if group == 10:
        if variant == 0:
            return [read(BIG, seed(0)), read(BIG + 0x1FF000, seed(0x1FF000)),
                    store(BIG, 0x1111222233334444), store(BIG + 0x1FF000, 0x5555666677778888),
                    read(BIG, 0x1111222233334444), read(BIG + 0x1FF000, 0x5555666677778888), store(BIG, seed(0)),
                    store(BIG + 0x1FF000, seed(0x1FF000)), fault(0, BIG + 0x200000, 0),
                    store(VA + 4092, 0x1122334455667788), read(VA + 4092, 0x1122334455667788),
                    fault(1, VA + 4092, 3)]
        return [read(BIG, seed(A)), read(BIG + 0x1FF000, seed(B)), read(BIG, seed(0)),
                read(BIG + 0x1FF000, seed(0x1FF000)), fault(0, BIG, 9), fault(0, BIG, 9)]
    if group == 11:
        return ([read(VA, seed(A)), read(HIGH, seed(A)), store(ALT, 0), store(ALT, 0x1100),
                 read(ALIAS + SPARE, 0x1100), store(ALIAS + SPARE, 0x2200), read(ALT, 0x2200),
                 store(ALT, 0)] if variant == 0 else [read(VA, seed(B))])
    raise ValueError("unknown finite-case group")


def operation_semantics(group, variant, cpu, actual):
    if group == 6:
        for operation in actual:
            entries = operation["end_walk"]["walked"]
            require(all(entry["value"] & 32 for entry in entries)
                    and bool(entries[-1]["value"] & 64) == (operation["begin"][1] == 1),
                    "finite read/write A/D effects")
    if group == 7:
        atomics = [p for p in actual if p["begin"][1] == 2 and p["end"][2] == 0]
        for index, operation in enumerate(atomics):
            equal = variant == 0 and index % 2 == 0
            require((operation["begin"][4] == operation["end"][1]) == equal,
                    "missing equality/mismatch atomic control")
        if variant == 1 and cpu == 0:
            operation = atomics[0]
            require(operation["initial_walk"]["walked"][-1]["value"] & 96 == 0
                    and operation["end_walk"]["walked"][-1]["value"] & 96 == 96,
                    "mismatch did not exercise fresh mapping A/D")
    if group == 11 and variant == 0:
        stores = [p for p in actual if p["begin"][1] == 1]
        require([bool(p["mutations"]) for p in stores] == [True, False, False, True],
                "table retirement/relink did not change native alias trapping")
    for index, operation in enumerate(actual):
        if operation["end"][2]:
            continue
        begin, end_walk = operation["begin"], operation["end_walk"]
        address, kind = begin[:2]
        physical = None
        if group == 1:
            physical = (A if address in (VA, 0, 0xFFFF800000000000) else B) + (address & 4095)
            if kind == 1 or address == ALIAS + A + cpu * 4096:
                physical = A + cpu * 4096
        elif group == 2:
            physical = ((A if variant < 16 else 0x5000 if variant == 20 else 0x4000) + (address & 4095))
        elif group == 3:
            physical = 0x4000 if kind == 3 else A
        elif group in (4, 12):
            physical = (0x4000 if index == 0 else 0x5000) if variant == 2 else A if variant == 0 and index == 0 else B
        elif group in (5, 6):
            physical = B if group == 5 else A
        elif group == 7:
            physical = operation["end"][1] & 0xFFFFF if address == VA else address - ALT + SPARE if address >= ALT else address - ALIAS
        elif group == 8:
            physical = address - ALIAS if address >= ALIAS else operation["end"][1] & 0xFFFFF if kind == 0 else A
        elif group == 9:
            physical = operation["end"][1] & 0xFFFFF
        elif group == 10:
            physical = address - BIG if address >= BIG else A + (address - VA)
            if variant == 1 and index < 2:
                physical = A if index == 0 else B
        elif group == 11:
            physical = (B if variant else A) if address in (VA, HIGH) else SPARE
        require(physical is None or end_walk["gpa"] == physical,
                f"case {group}/{variant}: independent expected physical address")
        if group == 10 and address >= BIG:
            require(end_walk["leaf_size"] == (4096 if variant == 1 and index < 2 else 0x200000),
                    "large-page/4KiB transition coverage")


def mutation_semantics(group, variant, guest_writes, path):
    if group == 1 and variant:
        changes = [m for m in guest_writes if m[0] == path[variant - 1]]
        require(len(changes) == 3 and not changes[0][2] & 1 and changes[1][2] & (1 << 36)
                and changes[2][2] & 1 and not changes[2][2] & (1 << 36), "absent/reserved at each walk level")
    if group == 2:
        if variant < 16:
            changes = [m for m in guest_writes if m[0] == path[variant // 4]]
            require(len(changes) == 3 and changes[0][2] & 7 == 5 and changes[1][2] & 7 == 3
                    and changes[2][2] & 7 == 7, "leaf/nonleaf RW/US restriction inputs")
        elif variant < 20:
            require(any(m[0] == path[variant - 16] and m[2] >> 63 for m in guest_writes),
                    "missing NX restriction at specified level")
    if group == 3:
        changes = [m for m in guest_writes if m[0] == path[variant]]
        require(len(changes) >= 8 and not changes[0][2] & 1 and changes[1][2] & 1
                and changes[2][2] & 7 == 5 and changes[3][2] & 7 == 7
                and changes[4][2] & 7 == 3 and changes[5][2] & 7 == 7
                and any(m[2] >> 63 for m in changes), "present/RW/US/NX no-invalidate relaxation inputs")
    if group == 6 and variant < 2:
        require(all(any(m[0] == address and m[2] & 96 == 0 for m in guest_writes) for address in path),
                "missing explicit A/D clear at a paging level")
    if group == 11 and variant == 1:
        recursive = [m for m in guest_writes if m[0] == 0x200000 + 510 * 8]
        require(len(recursive) == 2 and recursive[0][2] == 0x200007 and recursive[1][2] == 0,
                "recursive/multilevel table-use inputs")


def contention_coverage(records, operations, cases, ncpus, timer_bounds):
    contention_records = {variant: [] for variant in range(5)}
    progress = [[] for _ in range(ncpus)]
    alias_overlap = set()
    for (group, variant, rep), (start, stop) in cases.items():
        inside = [p for p in operations if start < p["sequence"] < stop]
        if group == 7 and variant == 0:
            atomics = [p for p in inside if p["begin"][1] == 2]
            readers = [p for p in inside if p["begin"][:2] == (VA, 0)]
            for atomic in atomics:
                if any(reader["cpu"] != atomic["cpu"] and
                       any(atomic["sequence"] < stop and start < atomic["end_sequence"]
                           for start, stop in reader["native_intervals"]) for reader in readers):
                    alias_overlap.add(atomic["begin"][0])
        if group == 8:
            events = [r for r in records if start < r[0] < stop]
            contention_records[variant].extend(events)
            for cpu in range(ncpus):
                marks = [r for r in events if r[1] == MARK and r[2] == cpu and r[4][0] == 8]
                require([r[4][:3] for r in marks] == [(8, 1, variant), (8, 0, variant)], "contended loop progress markers")
                own = [p for p in inside if p["cpu"] == cpu]
                require(marks[0][0] < own[0]["sequence"] and own[-1]["end_sequence"] < marks[1][0],
                        "contended operations outside loop progress markers")
                require(marks[0][4][3] <= marks[1][4][3], "contended timer count regressed")
                progress[cpu].append((marks[0][4][3], marks[1][4][3]))
    offset = ((VA >> 12) & 511) * 8
    require(alias_overlap == {ALIAS + SPARE + offset, ALT + offset},
            "no observed peer-native overlap with atomic requests through both table aliases")
    for variant, events in contention_records.items():
        waits = {}
        for event in events:
            if event[1] == WAIT and event[4][1] == 1 and event[4][2] > 0:
                waits.setdefault((event[2], event[4][0]), event[0])
        acknowledged = any(r[1] == ACK and r[4][2] == r[4][3] == 1 and r[4][5] > 0
                           and waits.get((r[2], r[4][4]), r[0]) < r[0] for r in events)
        require(acknowledged, f"group 8 variant {variant}: no observed fault/writer-wait remote acknowledgement")
        running, overlap = set(), False
        for event in events:
            if event[1] == MARK and event[4][0] == 6:
                if event[4][1]:
                    overlap |= bool(running - {event[2]})
                    running.add(event[2])
                else:
                    running.discard(event[2])
        require(overlap, f"group 8 variant {variant}: no observed overlapping native intervals")
    require(timer_bounds is not None, "contended timer context missing")
    for cpu, values in enumerate(progress):
        flattened = [value for pair in values for value in pair]
        require(flattened == sorted(flattened) and timer_bounds[cpu][0] <= flattened[0] < flattened[-1] <= timer_bounds[cpu][1],
                f"CPU {cpu}: no timer advance during contended stage or timer bounds mismatch")
        require(any(end > start for start, end in values),
                f"CPU {cpu}: timer advanced only between contended loops")


def case_coverage(records, operations, ncpus, contention=True, timer_bounds=None):
    required = {(group, variant, rep) for group, count in VARIANTS.items()
                for variant in range(count) for rep in range(16)}
    cases, active = {}, None
    accounted = set()
    path = [base + ((VA >> shift) & 511) * 8 for base, shift in
            ((0x200000, 39), (0x201000, 30), (0x202000, 21), (0x203000, 12))]
    for sequence, kind, cpu, _, data in records:
        if kind != CASE:
            continue
        key = data[:3]
        require(cpu == 0 and key in required and data[3] in (0, 1) and not any(data[4:]),
                f"record {sequence}: unknown case/version")
        if data[3]:
            require(active is None and key not in cases, f"record {sequence}: nested/duplicate case")
            active = key
            cases[key] = [sequence, None]
        else:
            require(active == key, f"record {sequence}: unmatched case END")
            cases[key][1] = sequence
            active = None
    require(active is None and set(cases) == required, "missing finite-case variants/repetitions")
    for (group, variant, rep), (start, stop) in cases.items():
        inside = [p for p in operations if start < p["sequence"] < stop]
        accounted.update(p["sequence"] for p in inside)
        require(all(p["end_sequence"] < stop and p["begin"][7] == (group << 32) | (rep << 16) | variant
                    for p in inside), f"case {group}/{variant}/{rep}: operation escaped case boundary")
        for cpu in range(ncpus):
            expected = case_operations(group, variant, cpu, ncpus)
            addresses = {item[1] for item in expected}
            actual = [p for p in inside if p["cpu"] == cpu and p["begin"][0] in addresses]
            require(len(actual) == len(expected), f"case {group}/{variant}/{rep} CPU {cpu}: missing/extra native operations")
            for operation, wanted in zip(actual, expected):
                begin, end = operation["begin"], operation["end"]
                require((begin[1], begin[0]) == wanted[:2]
                        and (wanted[2] is None or (end[2], end[3]) == wanted[2:4])
                        and (wanted[4] is None or end[1] == wanted[4]),
                        f"case {group}/{variant}/{rep} CPU {cpu}: wrong finite native input/outcome")
                require(begin[2] == (1 if group == 7 and begin[1] == 1 and begin[3] == 0x67 else 8),
                        "finite native operation width")
                if group == 7 and wanted[4] is None and begin[1] == 0:
                    require(end[1] in (0x5A00000000000000 | A, 0x5A00000000000000 | B), "alias peer walk outcome")
            if group == 8:
                own = [p for p in inside if p["cpu"] == cpu]
                require(len(own) == (144 if variant == 4 else 112) and sum(bool(p["mutations"]) for p in own) == 48
                        and sum(p["begin"][1] == 2 for p in own) == 16,
                        f"case 8/{variant}/{rep}: missing actual competing stores/atomics")
                entry, first, second = contended_entry(variant, cpu)
                for operation in actual:
                    begin, end = operation["begin"], operation["end"]
                    if begin[1] == 0:
                        flags, clear = (0x405, 98) if variant == 4 else (0x407, 96)
                        require((end[1] & ~clear) in (first | flags, second | flags) if begin[0] == ALIAS + entry else
                                end[1] in (0x5A00000000000000 | A, 0x5A00000000000000 | B),
                                "contended read returned foreign/hybrid data")
                    elif begin[1] == 2:
                        require(begin[3:5] == (second | 0x407, first | 0x407), "contended atomic input")
                    elif variant == 4 and begin[0] == VA:
                        require(begin[3] == 0x5A00000000000000 | A and (end[2], end[3]) in ((0, 0), (14, 3)),
                                "concurrent permission store input/outcome")
            if group == 9:
                roots = [0x220000, 0x220000, 0x200000] if cpu & 1 else [0x200000, 0x200000, 0x220000]
                require([p["context"] for p in actual] == [(root, 0, 1, 1, variant) for root in roots],
                        "CR3/PGE per-vCPU context coverage")
            if group == 2 and variant < 16:
                mode = variant % 4
                require(all(p["context"] == (0x200000, 3 if mode >= 2 else 0, mode & 1, 1, 0) for p in actual),
                        "effective permission context coverage")
            if contention:
                operation_semantics(group, variant, cpu, actual)
        if contention:
            guest_writes = [mutation for operation in inside for mutation in operation["mutations"]]
            mutation_semantics(group, variant, guest_writes, path)
    if contention:
        contention_coverage(records, operations, cases, ncpus, timer_bounds)
    require(len(accounted) == len(operations), "native operations outside enumerated cases")
    return len(cases)


def foundation(ram, cpus, sym, terminal=True, version=2):
    """Check the saved foundation and its continuing mappings/timer/TSO."""
    header = struct.unpack_from("<6Q", ram, 0x800000)
    require(version in (2, 3, 4, 5, 6), "foundation envelope version")
    ram_bytes = {2: RAM_BYTES, 3: RAM_BYTES_V3, 4: RAM_BYTES_V4, 5: RAM_BYTES_V5, 6: RAM_BYTES_V6}[version]
    alias = ALIAS_V6 if version == 6 else ALIAS_V5 if version == 5 else ALIAS_V4 if version == 4 else 0x60000000
    require(len(ram) == ram_bytes, "foundation RAM envelope size")
    require(header[:4] == (0x4E4154454D553031, 1, len(cpus), 1)
            and 16 <= header[4] < 32 and header[5] > 0, "foundation identity/timer")
    require(word(ram, 0x900000) == 0x13579BDF2468ACE0, "foundation sentinel changed")
    tcr = 16 | (1 << 8) | (1 << 10) | (3 << 12) | (16 << 16) | (1 << 24) | (1 << 26) | (3 << 28) | (2 << 30) | (1 << 32)
    for cpu, state in enumerate(cpus):
        saved = dict(zip(FOUNDATION_FIELDS, state["foundation"][:22]))
        live = dict(zip(FOUNDATION_FIELDS, struct.unpack_from("<22Q", ram, 0x800000 + 48 + 944 * cpu)))
        require(saved["ready"] == 1 and saved["done"] == saved["failure"] == saved["tso_end"] == 0,
                f"CPU {cpu}: foundation snapshot readiness")
        require(saved["tso_start"] & 2 and saved["sctlr"] & 0x1005 == 0x1005
                and saved["tcr"] == state["final_tcr"] == tcr
                and (saved["mmfr0"] >> 28) & 15 != 15, f"CPU {cpu}: foundation TSO/MMU")
        require(saved["sync_count"] == saved["sgi_count"] == 1 and saved["timer_count"] >= 3
                and state["timer_start"] == saved["timer_count"]
                and live["timer_count"] >= state["timer_end"] > state["timer_start"],
                f"CPU {cpu}: foundation interrupt continuation")
        require(saved["alias_value"] == 0x13579BDF2468ACE0 and saved["esr"] >> 26 == 0x25
                and saved["esr"] & 0x7FF == 7 and saved["esr"] & (1 << 25)
                and saved["far"] == alias + 4096 and saved["elr"] == sym["_fault_site"],
                f"CPU {cpu}: exact foundation native fault")
        require(saved["timer_intid"] == header[4] and not saved["waker_after"] & 6,
                f"CPU {cpu}: foundation GIC wake/timer")
        count = saved["trace_count"]
        require(count == saved["timer_count"] + 3 and count <= 32, f"CPU {cpu}: foundation snapshot trace")
        trace = [state["foundation"][22 + 3 * i:25 + 3 * i] for i in range(count)]
        require(trace[0][:2] == (1, saved["alias_value"]) and trace[1][:2] == (2, saved["esr"])
                and all(e[0] == 3 for e in trace[2:])
                and [e[1] for e in trace[2:]].count(1) == 1
                and [e[1] for e in trace[2:]].count(header[4]) == saved["timer_count"],
                f"CPU {cpu}: foundation trace effects")
        for key in FOUNDATION_FIELDS:
            if key not in ("done", "tso_end", "timer_count", "trace_count", "ticks_end"):
                require(live[key] == saved[key], f"CPU {cpu}: foundation field changed: {key}")
        if terminal:
            require(live["done"] == 1 and live["tso_end"] & 2, f"CPU {cpu}: final foundation completion/TSO")
        else:
            require(live["done"] == live["tso_end"] == live["ticks_end"] == 0,
                    f"CPU {cpu}: checkpoint incorrectly claims terminal foundation completion")
        expected_trace = [event[:2] for event in trace]
        expected_trace += [(3, header[4])] * (live["timer_count"] - saved["timer_count"])
        if terminal:
            expected_trace.append((4, live["timer_count"]))
        require(len(expected_trace) == live["trace_count"], f"CPU {cpu}: final foundation trace count")
        for logical in range(max(0, len(expected_trace) - 32), len(expected_trace)):
            address = 0x800000 + 48 + 944 * cpu + 176 + (logical % 32) * 24
            require(struct.unpack_from("<2Q", ram, address) == expected_trace[logical],
                    f"CPU {cpu}: retained foundation ring tail differs at logical event {logical}")
        pool = 0x40200000 + cpu * 0x80000
        require(state["final_ttbr0"] == pool | ((cpu + 1) << 48)
                and state["final_ttbr1"] == pool + 4096 and word(ram, pool - 0x40000000) == sym["_l1"] | 3,
                f"CPU {cpu}: native roots lost foundation")
    foundation_tables(ram, sym, version)


def foundation_tables(ram, sym, version):
    """Exact native foundation geometry; v5 alone introduces 2MiB identity blocks."""
    require(version in (2, 3, 4, 5, 6), "native table version")
    ram_bytes = {2: RAM_BYTES, 3: RAM_BYTES_V3, 4: RAM_BYTES_V4, 5: RAM_BYTES_V5, 6: RAM_BYTES_V6}[version]
    alias = ALIAS_V6 if version == 6 else ALIAS_V5 if version == 5 else ALIAS_V4 if version == 4 else 0x60000000
    small_tables = 8 if version >= 5 else ram_bytes // 0x200000
    l2 = {i: (sym["_pages"] + i * 4096) | 3 for i in range(small_tables)}
    if version >= 5:
        l2.update({i: (0x40000000 + i * 0x200000) | 0x701 for i in range(8, ram_bytes // 0x200000)})
        l2[ram_bytes // 0x200000] = sym["_aliases"] | 3
    else:
        l2[(alias >> 21) & 511] = sym["_aliases"] | 3
    l1 = {0: sym["_device_l2"] | 3, 1: sym["_l2"] | 3}
    if version >= 5:
        l1[2] = (sym["_l2"] + 4096) | 3
    tables = {"_l1": l1, "_l2": l2, "_aliases": {0: 0x40900703},
              "_device_l2": {128: 0x10000000 | 0x405 | (3 << 53),
                             **{136 + i: (0x11000000 + i * 0x200000) | 0x405 | (3 << 53) for i in range(8)}}}
    for name, entries in tables.items():
        length = 1024 if name == "_l2" and version >= 5 else 512
        require(all(word(ram, sym[name] - 0x40000000 + 8 * i) == entries.get(i, 0) for i in range(length)),
                f"foundation native table changed: {name}")
    require(all(word(ram, sym["_pages"] - 0x40000000 + 8 * i) == (0x40000000 + 4096 * i) | 0x703
                for i in range(small_tables * 512)), "foundation identity map changed")


def validate_native_faults(records, sym):
    requests = {r[0]: r[4] for r in records if r[1] == BEGIN}
    for sequence, kind, _, _, data in records:
        if kind != FAULT:
            continue
        begin = requests[data[0]]
        operation, width = begin[1:3]
        if operation == 3:
            targets = {begin[0] if begin[0] >> 63 else begin[0] | (1 << 47)}
        elif operation == 2:
            targets = {sym["_q30_cas_load"], sym["_q30_cas_store"]}
        else:
            targets = {sym["_q30_load8_site"] if width == 1 and operation in (0, 4) else
                       sym["_q30_load_site"] if operation == 0 else
                       sym["_q30_byte_site"] if width == 1 else sym["_q30_store_site"]}
        require(data[1] in targets and data[3] >> 26 == (0x21 if operation == 3 else 0x25),
                f"record {sequence}: native fault PC/instruction class")
        if operation != 3:
            write = operation == 1 or operation == 2 and data[1] == sym["_q30_cas_store"]
            require(bool(data[3] & 64) == write, f"record {sequence}: native fault WnR")


def check(ram, initial, finite_cases=True, sym=None, version=2, terminal=True):
    require(terminal or version >= 3, "nonterminal checkpoint requires explicit v3/v4 envelope")
    header, cpus, roles, records = decode(ram, initial, version)
    require(all(r[1] <= 12 for r in records), "Q31 continuation requires its independent metadata oracle")
    result = replay(initial, records, header["ncpus"], version)
    if sym is not None:
        foundation(ram, cpus, sym, terminal, version)
        validate_native_faults(records, sym)
    checked_cases = None
    if finite_cases:
        require(initial == initial_fixture(), "independent initial fixture bytes disagree")
        require(header["completed_groups"] == 0xFFF, "incomplete group bitmap")
        checked_cases = case_coverage(records, result["operations"], header["ncpus"],
                                      timer_bounds=[(cpu["timer_start"], cpu["timer_end"]) for cpu in cpus])
    validate_snapshot(ram, header, cpus, roles, result)
    if sym is None:
        result["incomplete"].add("foundation continuation needs retained Mach-O symbols")
    if not finite_cases:
        result["incomplete"].add("synthetic-only invocation omits finite execution coverage")
    return {"format": "natemu-q30-independent-check-v2" if version == 2 else f"natemu-q30-checkpoint-check-v{version}",
            "status": "INCOMPLETE" if result["incomplete"] else "PASS",
            "records": len(records), "operations": len(result["operations"]), "epochs": result["epoch"], "finite_cases": checked_cases,
            "checked": ["raw layout/publication", "contexts", "gate/ACK protocol records", "serialized mutation bytes",
                        "independent walk/map rights and A/D", "table reachability/retirement", "bounded operation outcomes",
                        "native intervals and post-quiescence ACKs", "all reachable native guest mappings"],
            "unsupported": sorted(result["incomplete"])}


def validate_snapshot(ram, header, cpus, roles, result):
    """Compare an actual quiescent snapshot with full replay, without rewriting it."""
    require(header["epoch"] == result["epoch"] and header["owner"] == result["owner"]
            and roles == result["roles"], "final epoch/owner/roles disagreement")
    for index, cpu in enumerate(cpus):
        require(cpu["context"] == result["contexts"][index] and cpu["ack"] == result["epoch"],
                f"CPU {index} final context/ACK")
        for key, count in result["counts"][index].items():
            require(cpu[key] == count, f"CPU {index} {key} summary/raw disagreement")
        operations = [p for p in result["operations"] if p["cpu"] == index]
        if operations:
            operation = operations[-1]
            begin, end, request = operation["begin"], operation["end"], cpu["request"]
            expected = dict(zip(REQUEST, (operation["sequence"], *begin[:5], end[5], end[6],
                                         end[2], end[3], end[4], end[7], 0)))
            require(request == expected, f"CPU {index} final operation/raw request disagreement")
    if not result["incomplete"]:
        require(ram[X86_OFFSET:X86_OFFSET + RAM_SIZE] == result["memory"],
                "final physical memory differs: unlogged mutation or wrong native store")
    else:
        result["incomplete"].add("final physical-memory equality unavailable with unresolved native effects")
    for (cpu, gva), (slot, descriptor) in result["maps"].items():
        require(word(ram, slot - 0x40000000) == descriptor, "final shadow descriptor disagrees with MAP")
        native_va = gva if gva >> 63 else gva | (1 << 47)
        pool = 0x40200000 + cpu * 0x80000
        table = pool + (4096 if gva >> 63 else 0)
        for shift in (39, 30, 21):
            entry = word(ram, table - 0x40000000 + ((native_va >> shift) & 511) * 8)
            require(entry & 3 == 3, "final native mapping lacks table path")
            table = entry & 0x0000FFFFFFFFF000
            require(pool <= table <= pool + 0x80000 - 4096, "native table escapes CPU shadow pool")
        require(table + ((native_va >> 12) & 511) * 8 == slot, "native MAP slot is not at reported VA")
    # Enumerate every reachable guest leaf, including leaves never logged by
    # the producer. Detached old pool pages are allowed after root eviction.
    observed = {}
    for cpu, state in enumerate(cpus):
        pool, visited = 0x40200000 + cpu * 0x80000, set()

        def descend(table, level, prefix, upper):
            require(table not in visited, "shared/cyclic guest shadow table")
            visited.add(table)
            for index, (entry,) in enumerate(struct.iter_unpack("<Q", ram[table - 0x40000000:table - 0x40000000 + 4096])):
                if level == 4 and not upper and index == 0:
                    continue  # Independently checked foundation root.
                if not entry:
                    continue
                require(level != 4 or upper or index >= 256, "guest leaf escaped lower native namespace")
                address = prefix | (index << (12 + 9 * (level - 1)))
                require(entry & 3 == 3, "guest shadow has unsupported block/invalid descriptor")
                if level == 1:
                    gva = address | 0xFFFF000000000000 if upper else address & ((1 << 47) - 1)
                    observed[cpu, gva] = table + index * 8, entry
                else:
                    child = entry & 0x0000FFFFFFFFF000
                    require(entry == child | 3 and pool + 8192 <= child < pool + state["shadow_used"] * 4096,
                            "guest shadow child outside allocated pool")
                    descend(child, level - 1, address, upper)

        descend(pool, 4, 0, False)
        descend(pool + 4096, 4, 0, True)
    require(observed == result["maps"], "reachable native guest leaves differ from validated MAP registry")
def check_run(run, continuation=None, version=None):
    version = (3 if continuation else 2) if version is None else version
    require(version in (2, 3, 4, 5, 6) and bool(continuation) == (version >= 3), "explicit retained envelope version")
    stage = {2: "q30-paging", 3: "q31faults", 4: "q32irq", 5: "q33code", 6: "q34startup"}[version]
    run = run.resolve()

    def artifact(name):
        path = run / name
        require(path.is_file() and not path.is_symlink() and run in path.resolve().parents, "artifact path " + name)
        return path

    checksums = {}
    for line in artifact("SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        require(name not in checksums and file_digest(artifact(name)) == digest,
                "duplicate/mismatched checksum " + name)
        checksums[name] = digest
    require({str(p.relative_to(run)) for p in run.rglob("*") if p.is_file()} == set(checksums) | {"SHA256SUMS"},
            "checksum inventory incomplete")
    manifest = json.loads(artifact("manifest.json").read_text())
    require(manifest["format"] == f"natemu-track01-run-v{version + 1}"
            and manifest["stage"] == stage
            and manifest["run_id"] == run.name and manifest["status"] == "executed", "retained run context")
    inputs, limits = manifest["inputs"], manifest["limits"]
    require(inputs["vcpus"] in (2, 4) and not inputs["build_only"] and inputs["entry_control"] is None
            and inputs["host_inherit_cpu_limit"] is True, "retained execution inputs")
    unlimited = [resource.RLIM_INFINITY] * 2
    require(limits["host_cpu_limit"] == unlimited and limits["command_wall_seconds"] == limits["guest_wall_seconds"] == 30
            and limits["build_child_cpu_seconds"] == 60
            and limits["child_file_bytes"] == ((2 << 30) if version >= 5 else (1 << 30) if continuation else 256 * 1024 * 1024),
            "retained resource bounds")
    commands = manifest["commands"]
    hosts = [c for c in commands if c["argv"][0] == str(run / "host")]
    require(len(hosts) == 1 and hosts[0] is commands[-1], "one final retained host invocation")
    for command in commands:
        require(command["returncode"] == 0 and command["status"] == "passed" and command["cwd"] == str(run)
                and command["timeout_seconds"] == 30 and command["cpu_limit"] == (unlimited if command is hosts[0] else [60, 60]),
                "retained command outcome/context")
    for name in ("guest.S", "guest.c", "x86.c", "q30.c", "q30_cases.c", "host.c") + (("q31.c",) if continuation else ()) + (("q32.c",) if version >= 4 else ()) + (("q33.c",) if version >= 5 else ()) + (("q34.c",) if version == 6 else ()):
        artifact("source/" + name)
        require(sum(str(run / "source" / name) in c["argv"] and c["argv"][:2] == ["xcrun", "clang"]
                    for c in commands) == 1, "single compilation from retained source: " + name)
    require(manifest["machine"] == "arm64" and manifest["sdk"]["version"] == "26.4", "prepared host/SDK scope")
    sym = symbols(artifact("guest.macho").read_bytes())
    require(hosts[0]["argv"] == [str(run / "host"), str(run / "guest.bin"), hex(sym["_start"]), str(inputs["vcpus"])],
            "retained host arguments")
    logs = [json.loads(line) for line in artifact(hosts[0]["stdout"]).read_text().splitlines()]
    require(not artifact(hosts[0]["stderr"]).read_bytes(), "nonempty host stderr")
    host_pass_key = f"q{version + 28}_host_checks_pass"
    require([r["stage"] for r in logs if "stage" in r] == [stage]
            and [r["gic_created"] for r in logs if "gic_created" in r] == [True]
            and [r["selected_el2_enabled"] for r in logs if "selected_el2_enabled" in r] == [False]
            and [r["stopped_ram_dump_complete"] for r in logs if "stopped_ram_dump_complete" in r] == [True]
            and [r[host_pass_key] for r in logs if host_pass_key in r] == [True],
            "retained host context/completion")
    setup = next(r for r in logs if "stage" in r)
    require((setup["q30_state_version"], setup["ram_bytes"], setup["x86_ram_bytes"], setup["ncpus"])
            == (version, {2: RAM_BYTES, 3: RAM_BYTES_V3, 4: RAM_BYTES_V4, 5: RAM_BYTES_V5, 6: RAM_BYTES_V6}[version], RAM_SIZE, inputs["vcpus"]),
            "host/raw setup disagreement")
    require(not any(any(k in r for k in ("canceled_pc", "error", "watchdog_timeout", "unexpected_virtual_timer_exit",
                                       "atexit_partial_ram_dump")) for r in logs), "failed/partial retained execution")
    ram = snapshot(artifact("ram.bin"), version)
    guest = artifact("guest.bin").read_bytes()
    text_end = min(sym[k] for k in ("_l1", "_l2", "_device_l2", "_pages", "_aliases")) - 0x40000000
    require(0 < text_end <= len(guest) and ram[:text_end] == guest[:text_end] == artifact("guest.macho").read_bytes()[:text_end],
            "retained/live guest text identity")
    start = sym["_start"] - 0x40000000
    require(struct.unpack_from("<I", ram, start)[0] == 0xD4000642, "entry HVC identity")
    stops = [0x40000000 + offset + 4 for offset in range(start, start + 128, 4)
             if struct.unpack_from("<I", ram, offset)[0] == 0xD4000602]
    require(len(stops) == 1, "completion HVC identity")
    checkpoints = [0x40000000 + offset + 4 for offset in range(0, text_end, 4)
                   if struct.unpack_from("<I", ram, offset)[0] == 0xD4000662] if continuation else []
    require(not continuation or len(checkpoints) == 1, "checkpoint HVC identity")
    q31_checkpoints = [0x40000000 + offset + 4 for offset in range(0, text_end, 4)
                       if struct.unpack_from("<I", ram, offset)[0] == 0xD4000682] if version >= 4 else []
    require(version < 4 or len(q31_checkpoints) == 1, "Q31 checkpoint HVC identity")
    q32_checkpoints = [0x40000000 + offset + 4 for offset in range(0, text_end, 4)
                       if struct.unpack_from("<I", ram, offset)[0] == 0xD40006A2] if version >= 5 else []
    transfers = [0x40000000 + offset + 4 for offset in range(0, text_end, 4)
                 if struct.unpack_from("<I", ram, offset)[0] == 0xD40006C2] if version >= 5 else []
    require(version < 5 or len(q32_checkpoints) == len(transfers) == 1, "Q32 checkpoint/transfer HVC identity")
    q33_checkpoints = [0x40000000 + offset + 4 for offset in range(0, text_end, 4)
                       if struct.unpack_from("<I", ram, offset)[0] == 0xD40006E2] if version == 6 else []
    require(version != 6 or len(q33_checkpoints) == 1, "Q33 checkpoint HVC identity")
    for cpu in range(inputs["vcpus"]):
        own = [r for r in logs if r.get("cpu") == cpu]
        initial = [r for r in own if "initial_actlr" in r]
        final = [r for r in own if "final_actlr" in r]
        exits = [r for r in own if "exit_reason" in r]
        require(len(initial) == len(final) == 1 and initial[0]["initial_actlr"] & 2
                and initial[0]["initial_sctlr"] == 0x30D00800 and final[0]["final_actlr"] & 2
                and final[0]["final_exec_ticks"] > 0 and final[0]["final_pc"] == stops[0], "owning-thread host CPU state")
        middle = [(1, 0x16, 0x36, transfers[0])] * (len(exits) - 5) if version == 5 else []
        if version == 6:
            between = exits[4:-1]
            positions = [i for i, r in enumerate(between) if r["syndrome"] & 65535 == 0x37]
            require(len(positions) == 1, "one owning-thread Q33 checkpoint")
            k = positions[0]
            middle = [(1, 0x16, 0x36, transfers[0])] * k + [(1, 0x16, 0x37, q33_checkpoints[0])] + [(1, 0x16, 0x36, transfers[0])] * (len(between) - k - 1)
        require([(r["exit_reason"], r["syndrome"] >> 26, r["syndrome"] & 65535, r["pc"]) for r in exits]
                == [(1, 0x16, 0x32, sym["_start"] + 4)]
                + ([(1, 0x16, 0x33, checkpoints[0])] if continuation else [])
                + ([(1, 0x16, 0x34, q31_checkpoints[0])] if version >= 4 else [])
                + ([(1, 0x16, 0x35, q32_checkpoints[0])] + middle if version >= 5 else [])
                + [(1, 0x16, 0x30, stops[0])], "host entry/checkpoint/completion exits")
        require([r["enter_run"] for r in own if "enter_run" in r] == list(range(len(exits) if version >= 5 else version)),
                "owning-thread run sequence")
        summaries = [r for r in own if "q30_started" in r]
        require(len(summaries) == 1, "unique Q30 CPU summary")
        raw_cpu = dict(zip(CPU, struct.unpack_from("<17Q", ram, STATE + 104 + CPU_WORDS * 8 * cpu)))
        for key in ("started", "done", "operations", "faults", "table_traps", "guest_faults", "ack", "timer_start", "timer_end"):
            require(summaries[0]["q30_" + key if key in ("started", "done") else key] == raw_cpu[key],
                    f"CPU {cpu}: summary/raw {key} disagreement")
        summaries = [r for r in own if "foundation_check" in r]
        live = dict(zip(FOUNDATION_FIELDS, struct.unpack_from("<22Q", ram, 0x800000 + 48 + 944 * cpu)))
        require(len(summaries) == 1 and summaries[0]["foundation_check"] is True
                and all(summaries[0][key] == live[key] for key in FOUNDATION_FIELDS
                        if key not in ("ready", "timer_intid", "redistributor")), "foundation summary/raw disagreement")
    require(word(ram, STATE + 16) == inputs["vcpus"], "RAM/manifest CPU count")
    initial = artifact("x86-initial.bin").read_bytes()
    report = continuation(artifact, logs, sym, ram, initial) if continuation else check(ram, initial, sym=sym)
    require(artifact("shared.bin").read_bytes() == ram[0x800000:0x800000 + 3824]
            and dump_equal(artifact("q30.bin"), ram, STATE, LOG_OFFSET + {2: CAPACITY, 3: CAPACITY_V3, 4: CAPACITY_V4, 5: CAPACITY_V5, 6: CAPACITY_V6}[version] * 96, version),
            "separate dumps disagree")
    report["checked"].extend([f"retained v{version + 1} artifacts/command context",
                              "preserved foundation mappings/timer/TSO"])
    return report


def self_check():
    """Fixed synthetic bytes/records, then one deliberate defect per rejection."""
    memory = bytearray(RAM_SIZE)
    for address, value in ((0x200000, 0x201007), (0x201000, 0x202007),
                           (0x202000, 0x203007), (0x203000, 0x100007), (0x100000, 1234)):
        put(memory, address, value)
    records = []

    def add(kind, cpu, epoch, *data):
        records.append((len(records) + 1, kind, cpu, epoch, tuple(data) + (0,) * (8 - len(data))))

    add(GATE, 0, 1, 1, 0)
    for page, role in ((0x200000, 8), (0x201000, 4), (0x202000, 2), (0x203000, 1)):
        add(ROLE, 0, 1, page, 0, role)
    add(CONTEXT_EVENT, 0, 1, *DEFAULT_CONTEXT)
    add(GATE, 0, 1, 2, 0)
    add(ACK, 0, 1, 1, 0, 0)
    add(ACK, 1, 1, 1, 4, 0)
    add(GATE, 0, 1, 0, 0)
    operation = len(records) + 1
    add(BEGIN, 0, 1, 0, 0, 8, 0, 0, 0, 0x202, 1)
    add(MARK, 0, 1, 6, 1, operation)
    add(FAULT, 0, 1, operation, 0x40000800, 0x800000000000, 0x96000007)
    add(MARK, 0, 1, 6, 0, operation)
    for address in (0x200000, 0x201000, 0x202000, 0x203000):
        old = word(memory, address)
        add(MUTATION, 0, 1, address, old, old | 32, 8, AD, operation, 32)
    add(MAP, 0, 1, 0, 0x100000, 0x40204000, 0x40B00F83 | (1 << 54), 4096, 15, 0, 0)
    add(MARK, 0, 1, 6, 1, operation)
    add(END, 0, 1, operation, 1234, 0, 0, 0x100000, 0, 0x202, 1)
    add(MARK, 0, 1, 6, 0, operation)
    result = replay(memory, records, 2)
    negatives = 0

    def rejects(label, mutate):
        nonlocal negatives
        changed = copy.deepcopy(records)
        mutate(changed)
        try:
            replay(memory, changed, 2)
        except ValueError:
            negatives += 1
        else:
            raise AssertionError(f"negative control accepted: {label}")

    def field(trace, kind, field, value, occurrence=0):
        index = [i for i, r in enumerate(trace) if r[1] == kind][occurrence]
        r = trace[index]
        data = list(r[4]); data[field] = value
        trace[index] = (*r[:4], tuple(data))

    rejects("wrong GPA", lambda r: field(r, MAP, 1, 0x101000))
    rejects("nonleaf rights omitted", lambda r: field(r, MAP, 5, 7))
    rejects("writable alias escape", lambda r: field(r, MAP, 6, 1))
    rejects("D on read", lambda r: field(r, MUTATION, 6, 96))
    rejects("wrong mutation old", lambda r: field(r, MUTATION, 1, 0))
    rejects("missing A", lambda r: r.pop(next(i for i, e in enumerate(r) if e[1] == MUTATION)))
    rejects("early remote ACK", lambda r: r.insert(6, r.pop(8)))
    rejects("missing remote ACK", lambda r: r.pop(8))
    rejects("stale post ACK load", lambda r: field(r, END, 1, 4321))
    rejects("wrong retirement", lambda r: field(r, ROLE, 2, 0))
    restricted = bytearray(memory)
    put(restricted, 0x201000, word(restricted, 0x201000) | (1 << 63))
    restricted_trace = copy.deepcopy(records)
    for index, event in enumerate(restricted_trace):
        if event[1] == MUTATION and event[4][0] == 0x201000:
            data = list(event[4]); data[1] |= 1 << 63; data[2] |= 1 << 63
            restricted_trace[index] = (*event[:4], tuple(data))
    try:
        replay(restricted, restricted_trace, 2)
    except ValueError:
        negatives += 1
    else:
        raise AssertionError("ignored real nonleaf NX restriction accepted")

    # The first fault installed a readable shadow. Later opcode-byte fetches
    # must retain their prevalidated physical identity even without a fault.
    def cached_fetches(last_gpa):
        trace = list(records)
        for address, value, physical in ((0, 1234 & 255, 0x100000), (1, 1234 >> 8, last_gpa)):
            op = len(trace) + 1
            for kind, data in ((BEGIN, (address, 4, 1, 0, 0, 0, 0x202, 1)),
                               (MARK, (6, 1, op)),
                               (END, (op, value, 0, 0, physical, 0, 0x202, 0)),
                               (MARK, (6, 0, op))):
                trace.append((len(trace) + 1, kind, 0, 1, tuple(data) + (0,) * (8 - len(data))))
        return trace
    for version in (3, 4, 5):
        assert not replay(memory, cached_fetches(0x100001), 2, version=version)["incomplete"]
    for version in (3, 4):
        assert not replay(memory, cached_fetches(MASK), 2, version=version)["incomplete"]
    for physical in (MASK, 0x100000):
        try:
            replay(memory, cached_fetches(physical), 2, version=5)
        except ValueError:
            negatives += 1
        else:
            raise AssertionError("v5 cached opcode fetch lost exact physical identity")

    # Exercise actual binary offsets, not only the record-level replay API.
    ram = bytearray(RAM_BYTES)
    header = [MAGIC, 2, 2, 0, len(records), 1, 0, 0, 0, 0, 0, 1, 0]
    struct.pack_into("<13Q", ram, STATE, *header)
    ram[X86_OFFSET:X86_OFFSET + RAM_SIZE] = result["memory"]
    ram[ROLES_OFFSET:LOG_OFFSET] = result["roles"]
    for cpu in range(2):
        values = [0] * CPU_WORDS
        for key, value in {"started": 1, "done": 1, "ack": 1, "shadow_used": 5,
                           "timer_start": 1, "timer_end": 2, **result["counts"][cpu]}.items():
            values[CPU.index(key)] = value
        values[17:22] = DEFAULT_CONTEXT
        if cpu == 0:
            end, begin = next(r[4] for r in reversed(records) if r[1] == END), records[10][4]
            values[22:35] = [operation, *begin[:5], end[5], end[6], end[2], end[3], end[4], end[7], 0]
        struct.pack_into(f"<{CPU_WORDS}Q", ram, STATE + 104 + cpu * CPU_WORDS * 8, *values)
    for event in records:
        struct.pack_into("<12Q", ram, LOG_OFFSET + (event[0] - 1) * 96, *event[:4], *event[4])
    # Four levels for native VA 0x800000000000; MAP is L3[0].
    put(ram, 0x200000 + 256 * 8, 0x40202003)
    put(ram, 0x202000, 0x40203003)
    put(ram, 0x203000, 0x40204003)
    put(ram, 0x204000, 0x40B00F83 | (1 << 54))
    assert check(ram, memory, finite_cases=False)["status"] == "INCOMPLETE"
    for address, value in ((LOG_OFFSET, 0), (STATE + 4 * 8, len(records) - 1),
                           (STATE + 104 + CPU.index("ack") * 8, 0),
                           (STATE + 104 + 22 * 8, operation + 1),
                           (0x204000, 0), (0x204008, 0x40B00F83 | (1 << 54))):
        changed = bytearray(ram)
        put(changed, address, value)
        try:
            check(changed, memory, finite_cases=False)
        except ValueError:
            negatives += 1
        else:
            raise AssertionError(f"raw corruption accepted at {address:#x}")
    case_records, case_ops, sequence = [], [], 0
    for group, count in VARIANTS.items():
        for rep in range(16):
            for variant in range(count):
                sequence += 1
                case_records.append((sequence, CASE, 0, 0, (group, variant, rep, 1, 0, 0, 0, 0)))
                for cpu in range(2):
                    expected = case_operations(group, variant, cpu, 2)
                    for index, (kind, address, status, error, value) in enumerate(expected):
                        sequence += 2
                        root = 0x200000
                        if group == 9:
                            root = ([0x220000, 0x220000, 0x200000] if cpu & 1 else
                                    [0x200000, 0x200000, 0x220000])[index]
                        result_value = value if value is not None else 0x5A00000000000000 | A
                        input_value, comparison = result_value, 0
                        if group == 8:
                            entry, first, second = contended_entry(variant, cpu)
                            if address == ALIAS + entry and kind in (0, 2):
                                result_value = first | (0x405 if variant == 4 and kind == 0 else 0x407)
                            if kind == 2:
                                input_value, comparison = second | 0x407, first | 0x407
                        width = 1 if group == 7 and kind == 1 and value == 0x67 else 8
                        begin = (address, kind, width, input_value, comparison, 0, 0x202, (group << 32) | (rep << 16) | variant)
                        case_ops.append(dict(sequence=sequence - 1, end_sequence=sequence, cpu=cpu, begin=begin,
                                             end=(sequence - 1, result_value, status or 0, error or 0, MASK, 0, 0x202, 0),
                                             context=(root, 3 if group == 2 and variant < 16 and variant % 4 >= 2 else 0,
                                                      variant % 2 if group == 2 and variant < 16 else 1, 1,
                                                      variant if group == 9 else 0),
                                             mutations=[1] if group == 8 and kind in (1, 2) and address != VA else []))
                sequence += 1
                case_records.append((sequence, CASE, 0, 0, (group, variant, rep, 0, 0, 0, 0, 0)))
    assert case_coverage(case_records, case_ops, 2, contention=False) == 864
    for bad_records, bad_ops in ((case_records[:-2], case_ops), (case_records, case_ops[1:])):
        try:
            case_coverage(bad_records, bad_ops, 2, contention=False)
        except ValueError:
            negatives += 1
        else:
            raise AssertionError("missing case/native operation coverage accepted")
    # A peer may see an A store before its serialized MUTATION log allocation.
    # Requiring that same peer to see the old word afterwards is inconsistent.
    ad_memory = bytearray(memory)
    for address in (0x200000, 0x201000, 0x202000):
        put(ad_memory, address, word(ad_memory, address) | 32)
    put(ad_memory, 0x203008, 0x203027)

    def ad_trace(regress=False):
        trace = []

        def emit(kind, cpu, *data):
            sequence = len(trace) + 1
            trace.append((sequence, kind, cpu, 1, tuple(data) + (0,) * (8 - len(data))))
            return sequence

        emit(GATE, 0, 1, 0)
        for address, role in ((0x200000, 8), (0x201000, 4), (0x202000, 2), (0x203000, 1)):
            emit(ROLE, 0, address, 0, role)
        emit(GATE, 0, 2, 0); emit(ACK, 0, 1, 0, 0); emit(ACK, 1, 1, 4, 0)
        emit(GATE, 0, 0, 0)

        def start(cpu, va, fault):
            op = emit(BEGIN, cpu, va, 0, 8, 0, 0, 0, 0x202, 1)
            emit(MARK, cpu, 6, 1, op)
            if fault:
                emit(FAULT, cpu, op, 0x40000800, (1 << 47) | va, 0x96000007)
                emit(MARK, cpu, 6, 0, op)
            return op

        def finish(cpu, op, value, gpa=MASK, faults=0):
            emit(END, cpu, op, value, 0, 0, gpa, 0, 0x202, faults)
            emit(MARK, cpu, 6, 0, op)

        op = start(0, 4096, True)
        emit(MAP, 0, 4096, 0x203000, 0x40204008, 0x40C03F83 | (1 << 54), 4096, 15, 0, 1)
        emit(MARK, 0, 6, 1, op)
        finish(0, op, 0x100007, 0x203000, 1)
        writer = start(1, 0, True)
        op = start(0, 4096, False)
        finish(0, op, 0x100027)
        if regress:
            op = start(0, 4096, False)
            finish(0, op, 0x100007)
        emit(MUTATION, 1, 0x203000, 0x100007, 0x100027, 8, AD, writer, 32)
        emit(MAP, 1, 0, 0x100000, 0x40284000, 0x40B00F83 | (1 << 54), 4096, 15, 0, 0)
        emit(MARK, 1, 6, 1, writer)
        finish(1, writer, 1234, 0x100000, 1)
        return trace

    replay(ad_memory, ad_trace(), 2)
    active_prefix = ad_trace()[:11]
    active_prefix += [(12, GATE, 1, 2, (1, 1, 0, 0, 0, 0, 0, 0)),
                      (13, GATE, 1, 2, (2, 1, 0, 0, 0, 0, 0, 0))]
    for bad in (ad_trace(True), active_prefix):
        try:
            replay(ad_memory, bad, 2)
        except ValueError:
            negatives += 1
        else:
            raise AssertionError("A/D new-then-old or pre-quiescence gate accepted")
    def changed_epoch(retire=False):
        trace = list(records)

        def emit(kind, cpu, *data):
            sequence = len(trace) + 1
            trace.append((sequence, kind, cpu, 2, tuple(data) + (0,) * (8 - len(data))))
            return sequence

        emit(GATE, 0, 1, 0)
        address, value = (0x202000, 0) if retire else (0x203000, 0x101007)
        emit(MUTATION, 0, address, word(result["memory"], address), value, 8, SETUP, operation, 0)
        if retire:
            emit(ROLE, 0, 0x203000, 1, 0)
        emit(GATE, 0, 2, 0); emit(ACK, 0, 2, 0, 0); emit(ACK, 1, 2, 4, 0)
        emit(GATE, 0, 0, 0)
        if not retire:
            op = emit(BEGIN, 0, 0, 0, 8, 0, 0, 0, 0x202, 1)
            emit(MARK, 0, 6, 1, op)
            emit(FAULT, 0, op, 0x40000800, 1 << 47, 0x96000007)
            emit(MARK, 0, 6, 0, op)
            emit(MUTATION, 0, 0x203000, 0x101007, 0x101027, 8, AD, op, 32)
            emit(MAP, 0, 0, 0x101000, 0x40204000, 0x40B01F83 | (1 << 54), 4096, 15, 0, 0)
            emit(MARK, 0, 6, 1, op)
            emit(END, 0, op, 0, 0, 0, 0x101000, 0, 0x202, 1)
            emit(MARK, 0, 6, 0, op)
        return trace

    retired, replaced = changed_epoch(True), changed_epoch()
    replay(memory, retired, 2); replay(memory, replaced, 2)
    bad_retirement = [event for event in retired if not (event[1] == ROLE and event[3] == 2)]
    bad_stale = copy.deepcopy(replaced)
    field(bad_stale, END, 1, 1234, occurrence=1)
    for bad in (bad_retirement, bad_stale):
        try:
            replay(memory, bad, 2)
        except ValueError:
            negatives += 1
        else:
            raise AssertionError("persistent retired classification or stale post-replacement-ACK read accepted")

    def reject_call(call):
        nonlocal negatives
        try:
            call()
        except ValueError:
            negatives += 1
        else:
            raise AssertionError("shared coverage helper accepted its negative control")

    def pending_write_trace(kind=1, equal=False, tracked=True):
        # The retained group-8 pattern: a peer has faulted on a table alias;
        # a reader completes before the peer's exclusive mutation. A mismatch
        # commits old->old, never the unused proposed replacement value.
        initial = bytearray(memory)
        for address in (0x200000, 0x201000, 0x202000, 0x203000):
            put(initial, address, word(initial, address) | 96)
        put(initial, 0x203008, 0x203067)
        va, gpa = (4096, 0x203000) if tracked else (0, 0x100000)
        old, proposed = word(initial, gpa), 0x101067 if tracked else 4321
        comparison = old if equal else 0
        final = proposed if kind == 1 or equal else old
        trace, epoch = list(records[:10]), 1

        def emit(event, cpu, *data):
            sequence = len(trace) + 1
            trace.append((sequence, event, cpu, epoch, tuple(data) + (0,) * (8 - len(data))))
            return sequence

        def start(cpu, access, value=0, compare=0, fault=True):
            op = emit(BEGIN, cpu, va, access, 8, value, compare, compare, 0x202, 1)
            emit(MARK, cpu, 6, 1, op)
            if fault:
                emit(FAULT, cpu, op, 0x40000800, (1 << 47) | va, 0x96000007 | (64 if access else 0))
                emit(MARK, cpu, 6, 0, op)
            return op

        def mapping():
            writable = int(not tracked)
            descriptor = (0x40A00000 + gpa) | 0xF03 | (1 << 54) | (0 if writable else 128)
            emit(MAP, 0, va, gpa, 0x40204000 + (va >> 12) * 8, descriptor, 4096, 15, writable, int(tracked))

        reader = start(0, 0)
        mapping()
        writer = start(1, kind, proposed, comparison if kind == 2 else 0, tracked)
        emit(MARK, 0, 6, 1, reader)
        emit(END, 0, reader, old, 0, 0, gpa, 0, 0x202, 1)
        emit(MARK, 0, 6, 0, reader)
        if tracked:
            epoch = 2
            emit(GATE, 1, 1, 1)
            emit(MUTATION, 1, gpa, old, final, 8, GUEST, writer, 0)
            emit(GATE, 1, 2, 1)
            emit(ACK, 1, 2, 0, 0)
            emit(ACK, 0, 2, 4, 0, 0, reader, 1)
            emit(GATE, 1, 0, 1)
            emit(MARK, 1, 6, 1, writer)
        emit(END, 1, writer, proposed if kind == 1 else old, 0, 0, gpa,
             0 if kind == 1 else old, 0x202 if kind == 1 else cmp_flags(comparison, old, 0x202), int(tracked))
        emit(MARK, 1, 6, 0, writer)
        if tracked:
            reader = start(0, 0)
            mapping()
            emit(MARK, 0, 6, 1, reader)
            emit(END, 0, reader, final, 0, 0, gpa, 0, 0x202, 1)
            emit(MARK, 0, 6, 0, reader)
        return initial, trace, gpa, final

    for kind, equal in ((1, False), (2, True), (2, False)):
        initial, trace, gpa, final = pending_write_trace(kind, equal)
        outcome = replay(initial, trace, 2)
        assert not outcome["incomplete"] and word(outcome["memory"], gpa) == final
        future_read = copy.deepcopy(trace)
        field(future_read, END, 1, 0x101067)
        reject_call(lambda: replay(initial, future_read, 2))
        forged = copy.deepcopy(trace)
        field(forged, MUTATION, 5, next(r[0] for r in trace if r[1] == BEGIN))
        reject_call(lambda: replay(initial, forged, 2))
        missing_commit = [r for r in trace if r[1] != MUTATION]
        reject_call(lambda: replay(initial, missing_commit, 2))
    initial, trace, _, _ = pending_write_trace(tracked=False)
    assert "overlapping native data read/write needs bounded replay" in replay(initial, trace, 2)["incomplete"]

    assert cmp_flags(0, 0, 0x202) == 0x246 and cmp_flags(0, 1, 0x202) == 0x297
    for compare, expected_flags in ((1234, 0x246), (0, 0x297)):
        atomic_trace = copy.deepcopy(records)
        for index, value in ((1, 2), (3, 4321), (4, compare), (5, compare)):
            field(atomic_trace, BEGIN, index, value)
        field(atomic_trace, MUTATION, 2, 0x100067, occurrence=3)
        field(atomic_trace, MUTATION, 6, 96, occurrence=3)
        field(atomic_trace, MAP, 3, 0x40B00F03 | (1 << 54))
        field(atomic_trace, MAP, 6, 1)
        field(atomic_trace, END, 5, 1234)
        field(atomic_trace, END, 6, expected_flags)
        outcome = replay(memory, atomic_trace, 2)
        assert word(outcome["memory"], 0x100000) == (4321 if compare == 1234 else 1234)
        field(atomic_trace, END, 6, 0x202)
        reject_call(lambda atomic_trace=atomic_trace: replay(memory, atomic_trace, 2))

    # These are the same semantic helpers called with contention=True above.
    semantic_path = [base + ((VA >> shift) & 511) * 8 for base, shift in
                     ((0x200000, 39), (0x201000, 30), (0x202000, 21), (0x203000, 12))]
    permissions = [(semantic_path[0], 0x201007, 0x201000 | new, 8) for new in (5, 3, 7)]
    mutation_semantics(2, 0, permissions, semantic_path)
    reject_call(lambda: mutation_semantics(2, 0, [(semantic_path[3], old, new, width)
                                                 for _, old, new, width in permissions], semantic_path))
    offset = ((VA >> 12) & 511) * 8
    old, compare = A | 0x67, MASK ^ (A | 0x67)
    atomic = dict(begin=(ALT + offset, 2, 8, 123, compare, compare, 0x202, 0),
                  end=(0, old, 0, 0, SPARE + offset, old, cmp_flags(compare, old, 0x202), 1),
                  initial_walk={"walked": [{"value": SPARE | 7}]},
                  end_walk={"walked": [{"value": SPARE | 0x67}], "gpa": SPARE + offset})
    operation_semantics(7, 1, 0, [atomic])
    for where, bits in (("initial_walk", SPARE | 0x67), ("end_walk", SPARE | 0x27)):
        bad = copy.deepcopy(atomic)
        bad[where]["walked"][0]["value"] = bits
        reject_call(lambda bad=bad: operation_semantics(7, 1, 0, [bad]))
    retirement = []
    store_index = 0
    for kind, address, status, error, value in case_operations(11, 0, 0, 2):
        trapped = kind == 1 and store_index in (0, 3)
        store_index += kind == 1
        retirement.append(dict(begin=(address, kind, 8, value, 0, 0, 0x202, 0),
                               end=(0, value, status, error, MASK, 0, 0x202, 0),
                               end_walk={"gpa": A if address in (VA, HIGH) else SPARE},
                               mutations=[1] if trapped else []))
    operation_semantics(11, 0, 0, retirement)
    bad = copy.deepcopy(retirement); bad[3]["mutations"] = [1]
    reject_call(lambda: operation_semantics(11, 0, 0, bad))
    bad_gpa = copy.deepcopy(retirement); bad_gpa[4]["end_walk"]["gpa"] = A
    reject_call(lambda: operation_semantics(11, 0, 0, bad_gpa))

    # Small synthetic instances exercise all contention gates without fabricating
    # a whole VM run. Full case counts are checked separately above.
    contention_cases = {(7, 0, 0): (0, 100)}
    contention_ops = [dict(sequence=10, end_sequence=40, cpu=0, begin=(ALIAS + SPARE + offset, 2)),
                      dict(sequence=50, end_sequence=80, cpu=0, begin=(ALT + offset, 2)),
                      dict(sequence=20, end_sequence=30, cpu=1, begin=(VA, 0), native_intervals=[(21, 29)]),
                      dict(sequence=60, end_sequence=70, cpu=1, begin=(VA, 0), native_intervals=[(61, 69)])]
    contention_events = []
    for variant in range(5):
        base = 100 + variant * 100
        contention_cases[8, variant, 0] = base, base + 100
        def event(delta, kind, cpu, *data):
            contention_events.append((base + delta, kind, cpu, 1, tuple(data) + (0,) * (8 - len(data))))
        event(1, MARK, 0, 8, 1, variant, 10 + variant)
        event(2, MARK, 1, 8, 1, variant, 10 + variant)
        event(11, MARK, 0, 6, 1, base + 9)
        event(12, MARK, 1, 6, 1, base + 10)
        event(13, FAULT, 1, base + 10, 0x40000800, 1 << 47, 0x96000007)
        event(14, MARK, 0, 6, 0, base + 9)
        event(15, MARK, 1, 6, 0, base + 10)
        event(16, WAIT, 1, base + 10, 1, 1, 10 + variant)
        event(17, ACK, 1, 1, 2, 1, 1, base + 10, 1)
        event(90, MARK, 0, 8, 0, variant, 11 + variant)
        event(91, MARK, 1, 8, 0, variant, 11 + variant)
        for cpu in range(2):
            contention_ops.append(dict(sequence=base + 9 + cpu, end_sequence=base + 80 + cpu, cpu=cpu, begin=(VA, 0)))
    bounds = [(9, 16), (9, 16)]
    contention_coverage(contention_events, contention_ops, contention_cases, 2, bounds)
    reject_call(lambda: contention_coverage(contention_events, contention_ops[1:], contention_cases, 2, bounds))
    for kind in (WAIT, ACK):
        bad_events = [e for e in contention_events if e[1] != kind]
        reject_call(lambda bad_events=bad_events: contention_coverage(bad_events, contention_ops, contention_cases, 2, bounds))
    no_overlap = [e for e in contention_events if not (e[1] == MARK and e[4][0] == 6 and e[2] == 1)]
    reject_call(lambda: contention_coverage(no_overlap, contention_ops, contention_cases, 2, bounds))
    stalled = []
    for e in contention_events:
        if e[1] == MARK and e[4][0] == 8:
            data = list(e[4]); data[3] = 10
            e = (*e[:4], tuple(data))
        stalled.append(e)
    reject_call(lambda: contention_coverage(stalled, contention_ops, contention_cases, 2, bounds))
    between_loops = []
    for e in contention_events:
        if e[1] == MARK and e[4][0] == 8:
            data = list(e[4]); data[3] = 10 + data[2]
            e = (*e[:4], tuple(data))
        between_loops.append(e)
    reject_call(lambda: contention_coverage(between_loops, contention_ops, contention_cases, 2, bounds))
    print(f"Q30 synthetic self-check PASS: raw image, concurrent A/D, 864 signatures, shared semantic/contention helpers, {negatives} rejected corruptions; no VM")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", nargs="?", type=Path)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    parser.error("run directory required") if args.run is None else None
    try:
        report = check_run(args.run)
    except (ValueError, OSError, KeyError, TypeError, struct.error) as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}, sort_keys=True))
        raise SystemExit(1)
    print(json.dumps(report, indent=2, sort_keys=True))
    raise SystemExit(0 if report["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()

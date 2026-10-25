"""Read actual pinned FEX allocation/emission maps; never invent root locations.

The admitted x86 shapes are PXOR/ADDPS; ADD; MOV RAX,[RAX]; SETZ; MOVDQU.
Both memory sites carry 35 current roots. The conservative recovery-copy profile
forces genuine allocator pressure; its measured locations/frame size come from
the producer trace and are validated against native memory instructions.

Caller supplies the combined producer stdout/stderr, the requested raw object,
its export index (1 for a fresh single request), and the exact submitted bytes.
The result contains body:bytes and JSON-compatible metadata. Runtime publication
must additionally bind object/frame/bank lifetime and restore its return target.
"""

import json
import re
import struct

ALIAS = bytes.fromhex("660fefc14801d1488b000f94c2f30f7f07")
FP_ALIAS = bytes.fromhex("0f58c14801d1488b000f94c2f30f7f07")
GPRS = ["RAX", "RBX", "RCX", "RDX", "RSI", "RDI", "RBP", "RSP"] + [f"R{i}" for i in range(8, 16)]
ROOTS = GPRS + [f"XMM{i}.low" for i in range(16)] + ["pf_raw", "af_raw", "nzcv"]
ENTRY_BINDINGS = {
    **{role: {"kind": "gpr", "register": reg}
       for role, reg in zip(GPRS, (4, 6, 7, 5, 10, 11, 9, 8, 12, 13, 14, 15, 16, 17, 19, 29))},
    **{f"XMM{i}.low": {"kind": "vector", "register": 16 + i} for i in range(16)},
    "pf_raw": {"kind": "gpr", "register": 26},
    "af_raw": {"kind": "gpr", "register": 27},
    "nzcv": {"kind": "native_nzcv", "carry_inverted": True},
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def make_input(source, label="request"):
    """One caller-supplied request; 77 is the observed count for this pinned profile."""
    require(source in (ALIAS, FP_ALIAS), "unsupported x86 source")
    require(type(label) is str and label.isascii() and 0 < len(label) < 128 and "\0" not in label,
            "invalid request label")
    return (struct.pack("<6Q", 64, 25, 1, 8192, 49155, 0) +
            struct.pack("<128sqQQI", label.encode(), 77, len(source), 5, 0x41424344) + source)


def _trace(log):
    require(type(log) is str and len(log) <= 1024 * 1024, "invalid or oversized producer log")
    attempts, objects, completion = [], [], []
    feature_records = 0
    current = None
    for line in log.splitlines():
        if not line.startswith("NATEMU_"):
            continue
        kind, _, rest = line.partition(" ")
        if kind == "NATEMU_OBJECT":
            objects.append(json.loads(rest))
            continue
        if kind == "NATEMU_COMPLETED":
            match = re.fullmatch(r"tests=(\d+) result=([01])", rest)
            require(match is not None, "malformed completion")
            completion.append(tuple(map(int, match.groups())))
            continue
        if kind == "NATEMU_FEATURES":
            require(rest == "sve128=0 sve256=0 rcpc=0 imm9=0 atomics=1 preserve_all=1", "wrong host features")
            feature_records += 1
            continue
        if kind == "NATEMU_COMPILE":
            continue  # Retained diagnostics, never a performance claim.
        require(kind in {"NATEMU_BEGIN", "NATEMU_STACK", "NATEMU_SPILL", "NATEMU_FILL",
                         "NATEMU_SITE", "NATEMU_ROOT", "NATEMU_FLAGS", "NATEMU_END"},
                "producer rejection or unknown metadata marker: " + kind)
        fields = dict(part.split("=", 1) for part in rest.split())
        if kind == "NATEMU_BEGIN":
            require(current is None, "abandoned/restarted emission is outside this profile")
            require(fields["format"] == "allocated35-pressure-v1", "wrong map format")
            current = {"entry": fields["entry"], "spills": [], "fills": [],
                       "sites": {}, "roots": {}, "flags": {}}
            continue
        require(current is not None and fields["entry"] == current["entry"], "unbracketed or cross-object map")
        if kind == "NATEMU_STACK":
            require("stack" not in current, "duplicate stack declaration")
            current["stack"] = (int(fields["slots"]), int(fields["bytes"]))
        elif kind in ("NATEMU_SPILL", "NATEMU_FILL"):
            current["spills" if kind == "NATEMU_SPILL" else "fills"].append(fields)
        elif kind in ("NATEMU_SITE", "NATEMU_FLAGS"):
            table = current["sites" if kind == "NATEMU_SITE" else "flags"]
            require(fields["access"] not in table, "duplicate access metadata")
            table[fields["access"]] = fields
        elif kind == "NATEMU_ROOT":
            roots = current["roots"].setdefault(fields["access"], {})
            require(fields["role"] not in roots, "duplicate root")
            roots[fields["role"]] = fields
        else:
            current["size"] = int(fields["size"])
            attempts.append(current)
            current = None
    require(current is None, "unterminated emission")
    require(feature_records == 1, "missing or duplicate host feature record")
    require(2 <= len(attempts) <= 7 and len(objects) == len(attempts), "missing or extra emitted objects")
    require([o["index"] for o in objects] == list(range(len(objects))), "wrong object sequence")
    require(len(completion) == 1 and completion[0][0] == len(attempts) - 1, "incomplete compile")
    if completion[0][1]:
        require("Fail: Test took " in log, "unexplained count-tool failure")
    return attempts, objects, completion[0][1]


def _register(value, prefix):
    require(re.fullmatch(prefix + r"\d+", value) is not None, "invalid native register")
    index = int(value[1:])
    require(0 <= index < (32 if prefix == "v" else 31), "native register outside class")
    return index


def _instruction_allowed(word):
    # The admitted leaf has no branch, helper, literal, or canonical-state access.
    return (word in (0xD503201F, 0xD5033BBF, 0xD500401F, 0x6E311E10, 0x4E31D610) or
            word & 0xFFC003FF in (0xD10003FF, 0x910003FF) or  # SP subtract/add
            word & 0xFFE0FFE0 == 0xAA0003E0 or  # MOV Xd,Xm
            word & 0xFFE0FC00 in (0xCA000000, 0xAB000000) or  # EOR/ADDS
            word & 0xFFC003E0 in (0xF90003E0, 0xF94003E0) or  # private spill/fill
            word & 0xFFFFFC00 in (0xC8DFFC00, 0xB3401C00) or  # LDAR/BFXIL low byte
            word & 0xFFFFFFE0 in (0xD53B4200, 0x9A9F17E0) or  # MRS NZCV/CSET EQ
            word & 0xFFC00000 == 0x3D800000)  # STR Q


def parse(log: str, raw: bytes, object_index: int, source: bytes) -> dict:
    require(source in (ALIAS, FP_ALIAS), "unsupported x86 source")
    require(type(raw) is bytes and 0 < len(raw) <= 65536 and len(raw) % 4 == 0, "invalid native object extent")
    attempts, objects, tool_status = _trace(log)
    require(type(object_index) is int and 1 <= object_index < len(attempts), "requested object missing")
    attempt, obj = attempts[object_index], objects[object_index]
    require(attempt["size"] == len(raw) == obj["bytes"] and obj["guest_instructions"] == 5,
            "object metadata does not match request")
    begin = obj["code_offset"]
    require(type(begin) is int and begin >= 4 and begin % 4 == 0, "invalid native code offset")
    require(type(obj["code_bytes"]) is int and begin + obj["code_bytes"] <= len(raw), "invalid native code extent")
    require(raw[begin:begin + 8] == bytes.fromhex("e0ffff10800300f9"), "unexpected bookkeeping prefix")
    body_start = begin + 8

    def word(offset):
        require(type(offset) is int and offset % 4 == 0 and 0 <= offset <= len(raw) - 4,
                "instruction outside object")
        return struct.unpack_from("<I", raw, offset)[0]

    stops = [at for at in range(body_start, begin + obj["code_bytes"], 4) if word(at) == 0x420F]
    require(stops, "missing semantic-body end marker")
    body_end = stops[0]
    slots, spill_bytes = attempt["stack"]
    require(0 < slots <= 64 and spill_bytes == slots * 32, "invalid spill frame")
    require(word(body_start) == (0xD10003FF | spill_bytes << 10), "stack allocation does not match map")
    require(word(body_end - 4) == (0x910003FF | spill_bytes << 10), "stack restoration does not match map")
    require(word(body_start + 4) == (0x6E311E10 if source == ALIAS else 0x4E31D610), "source/vector computation mismatch")
    carry_at = {}
    carry_inverted = True  # Fixed FEX entry ABI, overwritten by the guest ADD.
    for at in range(body_start, body_end, 4):
        require(_instruction_allowed(word(at)), f"instruction outside finite leaf profile at {at - body_start}")
        carry_at[at] = carry_inverted
        if word(at) & 0xFFE0FC00 == 0xAB000000:
            carry_inverted = False
        elif word(at) == 0xD500401F:
            carry_inverted = not carry_inverted
    stack_adjustments = [at for at in range(body_start, body_end, 4)
                         if word(at) & 0xFFC003FF in (0xD10003FF, 0x910003FF)]
    require(stack_adjustments == [body_start, body_end - 4], "unexpected interior SP adjustment")

    initialized = {}
    for witness in attempt["spills"]:
        slot, offset, width = int(witness["slot"]), int(witness["offset"]), int(witness["bytes"])
        reg = _register(witness["src"], "x")
        require(0 <= slot < slots and slot not in initialized and width == 8, "invalid or duplicate spill initialization")
        require(body_start <= offset < body_end, "spill witness outside body")
        require(word(offset) == (0xF90003E0 | (slot * 4) << 10 | reg), "spill witness does not match STR bytes")
        initialized[slot] = offset
    require(len(initialized) == slots, "allocated spill slot lacks initialization")
    for witness in attempt["fills"]:
        slot, offset, width = int(witness["slot"]), int(witness["offset"]), int(witness["bytes"])
        reg = _register(witness["src"], "x")
        require(slot in initialized and initialized[slot] < offset < body_end and width == 8, "uninitialized fill")
        require(word(offset) == (0xF94003E0 | (slot * 4) << 10 | reg), "fill witness does not match LDR bytes")
    for kind, opcode in (("spills", 0xF90003E0), ("fills", 0xF94003E0)):
        actual = [at for at in range(body_start, body_end, 4) if word(at) & 0xFFC003E0 == opcode]
        require(actual == [int(witness["offset"]) for witness in attempt[kind]], "unwitnessed private spill/fill access")

    require(len(attempt["sites"]) == 2 and set(attempt["sites"]) == set(attempt["roots"]) == set(attempt["flags"]),
            "missing access/map/flag association")
    maps = []
    for access, site in attempt["sites"].items():
        offset = int(site["offset"])
        store = site["dst"].startswith("v")
        reg = _register(site["dst"], "v" if store else "x")
        address = _register(site["address"], "x")
        require(body_start <= offset < body_end - 4, "access outside body")
        opcode = 0x3D800000 if store else 0xC8DFFC00
        require(word(offset) == (opcode | address << 5 | reg), "site does not match memory instruction")
        require(word(offset - 4 if store else offset + 4) == (0xD5033BBF if store else 0xD503201F),
                "missing emitted ordering instruction")
        roots, flags = attempt["roots"][access], attempt["flags"][access]
        require(set(roots) == set(ROOTS), "incomplete current-root catalog")
        guest_offset = (13 if store else 7) if source == ALIAS else (12 if store else 6)
        require(all(int(root["guest_offset"]) == guest_offset for root in roots.values()), "wrong guest phase")
        carry = int(flags["carry_inverted"])
        require(carry in (0, 1) and int(flags["native_fpsr"]) == int(source == FP_ALIAS), "wrong flag/status authority")
        require(bool(carry) == carry_at[offset], "carry recipe disagrees with actual flag-changing instructions")
        descriptors = []
        for role in ROOTS:
            location = roots[role]["location"]
            width = 16 if role.startswith("XMM") else 8
            if location.startswith("sp+"):
                require(re.fullmatch(r"sp\+\d+", location) is not None, "invalid spill location")
                slot_offset = int(location[3:])
                require(slot_offset % 32 == 0 and 0 <= slot_offset <= spill_bytes - width, "spill root outside frame")
                require(slot_offset // 32 in initialized and initialized[slot_offset // 32] < offset, "root lacks dominating spill")
                require(width == 8, "unsupported vector spill witness")
                descriptor = {"kind": "spill", "offset": slot_offset,
                              "initializing_native_offset": initialized[slot_offset // 32] - body_start}
            else:
                root_reg = _register(location, "v" if width == 16 else "x")
                require(store or width == 16 or root_reg != reg, "pre-load root aliases overwritten destination")
                descriptor = {"kind": "vector" if width == 16 else "gpr", "register": root_reg}
            descriptors.append({"role": role, "bytes": width, **descriptor})
        maps.append({"access": "store" if store else "load", "bytes": 16 if store else 8,
                     "native_offset": offset - body_start, "native_continuation": offset - body_start + 4,
                     "guest_offset": guest_offset, "address_register": address,
                     "value_register" if store else "destination_register": reg,
                     "phase": "precommit", "roots": descriptors, "carry_inverted": bool(carry),
                     "fpsr_authority": "captured-native-FPSR", "native_fp_prefix": source == FP_ALIAS})
    maps.sort(key=lambda item: item["native_offset"])
    require([item["access"] for item in maps] == ["load", "store"], "wrong memory effect order")
    native_memory_sites = [at - body_start for at in range(body_start, body_end, 4)
                           if word(at) & 0xFFFFFC00 == 0xC8DFFC00 or word(at) & 0xFFC00000 == 0x3D800000]
    require(native_memory_sites == [item["native_offset"] for item in maps], "unmapped guest memory access")
    return {"schema": "natemu.i3.allocated-map.v1", "body": raw[body_start:body_end],
            "source_hex": source.hex(), "extraction_range": [body_start, body_end],
            "spill_bytes": spill_bytes, "spill_slots": slots, "sites": maps,
            "root_order": list(ROOTS),
            "entry_bindings": {role: dict(binding) for role, binding in ENTRY_BINDINGS.items()},
            "canonical_state_accesses": [], "count_tool_status": tool_status,
            "profile": "conservative-recovery-copy-pressure",
            "stack_restored_by_body": True, "restore_runtime_return_target": True}

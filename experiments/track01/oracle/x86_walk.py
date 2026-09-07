#!/usr/bin/env python3
"""Independent Intel SDM 092 four-level, uncached x86 walk oracle (stdlib only).

Sources: Vol. 3A 5.5.2, 5.5.4/Table 5-15 through 5-20, 5.6, 5.7 and 5.8:
https://cdrdv2-public.intel.com/922487/253668-092-sdm-vol-3a.pdf

Fixture: 4 MiB GPA RAM, MAXPHYADDR=36, four-level paging, no PCID/1GiB,
SMEP/SMAP/PKE/PKS/CET/LAM/HLAT disabled, ordinary WB attributes only.
This models one linear address, not an instruction's multi-page atomicity,
cached/delayed-invalidation behavior, speculative walks, or guest frame delivery.
The caller must check every operand page and its instruction-specific effects.
It neither imports experiment code nor changes memory or executes a VM.
"""
import struct

RAM_SIZE = 4 * 1024 * 1024
PHYSICAL_MASK = (1 << 36) - 1
ADDRESS_MASK = PHYSICAL_MASK & ~0xFFF
RESERVED_ADDRESS = ((1 << 52) - 1) & ~PHYSICAL_MASK


def walk(memory_bytes, cr3, gva, access, cpl, wp, nxe):
    """Return status, GPA/leaf_size/rights, fault/error_code, walked, ad_required.

    status: ok, pf, gp, unsupported_ram, unsupported_attributes.
    rights contains cumulative rw/user/execute, plus effective read/write/fetch.
    walked records include a nonpresent/reserved terminal entry if readable.
    ad_required lists bits which must be set on a completed permitted uncached
    access (including already-set bits). It is empty on faults/incomplete walks;
    this does NOT require A bits to remain zero on fault or speculative paths.
    unsupported_ram with a GPA means translation/rights succeeded, but the GPA
    is outside this fixture. Its ad_required is conditional on completing the
    access. Missing table storage cannot be interpreted as architectural P=0.
    Non-WB attribute selectors are unsupported, not architectural reserved bits.
    Invalid supplied CR3/control state raises ValueError, not an access #GP.
    """
    if not isinstance(memory_bytes, (bytes, bytearray)) or len(memory_bytes) != RAM_SIZE:
        raise ValueError("expected exactly 4 MiB of raw guest physical RAM")
    if access not in ("read", "write", "execute") or cpl not in (0, 3):
        raise ValueError("access must be read/write/execute and CPL must be 0/3")
    if wp not in (False, True) or nxe not in (False, True):
        raise ValueError("WP/NXE must be boolean")
    if not isinstance(cr3, int) or not 0 <= cr3 <= PHYSICAL_MASK:
        raise ValueError("CR3 has unavailable/reserved physical or LAM bits")
    if not isinstance(gva, int) or not 0 <= gva < 1 << 64:
        raise ValueError("GVA must be an unsigned 64-bit value")
    result = {"status": None, "gpa": None, "leaf_size": None, "rights": None,
              "fault": None, "error_code": None, "walked": [], "ad_required": []}

    def finish(status, reason, error=None):
        result.update(status=status, reason=reason, error_code=error,
                      fault={"pf": "PF", "gp": "GP"}.get(status))
        return result

    # Both canonical halves require bits 63:47 to equal the sign of bit 47.
    if gva >> 47 not in (0, (1 << 17) - 1):
        return finish("gp", "noncanonical linear address", 0)
    if cr3 & 0x18:
        return finish("unsupported_attributes", "CR3 PWT/PCD outside WB fixture")
    table, rw, user, executable = cr3 & ADDRESS_MASK, True, True, True
    # SMEP=0 and PAE=1: I/D reports fetch only if NXE=1, even for RSVD faults.
    error_base = (2 if access == "write" else 0) | (4 if cpl == 3 else 0)
    error_base |= 16 if access == "execute" and nxe else 0
    attributes_supported = True
    for level, shift in ((4, 39), (3, 30), (2, 21), (1, 12)):
        entry_gpa = table + ((gva >> shift) & 0x1FF) * 8
        if entry_gpa + 8 > len(memory_bytes):
            result["unbacked_table_entry_gpa"] = entry_gpa
            return finish("unsupported_ram", "paging-structure storage outside fixture")
        value = struct.unpack_from("<Q", memory_bytes, entry_gpa)[0]
        result["walked"].append({"level": level, "entry_gpa": entry_gpa, "value": value})
        if not value & 1:
            return finish("pf", "nonpresent paging entry", error_base)
        large = level == 2 and bool(value & 0x80)
        reserved = value & RESERVED_ADDRESS or (not nxe and value & (1 << 63))
        reserved |= level in (4, 3) and bool(value & 0x80)
        reserved |= large and bool(value & 0x1FE000)  # PDE PS=1: bits 20:13.
        if reserved:
            return finish("pf", "reserved paging-entry bit", error_base | 9)
        rw &= bool(value & 2)
        user &= bool(value & 4)
        executable &= not bool(value & (1 << 63))
        attributes_supported &= not bool(value & 0x18)
        leaf_size = (1 << 21) if large else 4096 if level == 1 else None
        if leaf_size is None:
            table = value & ADDRESS_MASK
            continue
        attributes_supported &= not bool(value & (0x1000 if large else 0x80))
        rights = {"rw": rw, "user": user, "execute": executable,
                  "read": cpl == 0 or user,
                  "write": (cpl == 0 or user) and (rw or (cpl == 0 and not wp)),
                  "fetch": (cpl == 0 or user) and executable}
        result.update(leaf_size=leaf_size, rights=rights)
        if not rights[{"execute": "fetch"}.get(access, access)]:
            return finish("pf", "cumulative permission denial", error_base | 1)
        result["gpa"] = (value & PHYSICAL_MASK & ~(leaf_size - 1)) | (gva & (leaf_size - 1))
        if not attributes_supported:
            return finish("unsupported_attributes", "PWT/PCD/PAT selector outside WB fixture")
        for entry in result["walked"]:
            result["ad_required"].append({"entry_gpa": entry["entry_gpa"], "set_mask": 0x20})
        if access == "write":
            result["ad_required"][-1]["set_mask"] |= 0x40
        if result["gpa"] >= len(memory_bytes):
            return finish("unsupported_ram", "valid translated GPA outside fixture")
        return finish("ok", "permitted uncached translation")
    raise AssertionError("four-level walk did not reach a leaf")


def self_check():
    """Independent synthetic table bytes and fixed expected outcomes; no VM."""
    checks = 0

    def put(memory, address, value):
        struct.pack_into("<Q", memory, address, value)

    def fixture(gva=0x123456789ABC, large=False):
        memory = bytearray(RAM_SIZE)
        addresses = []
        for table, shift, target in ((0x1000, 39, 0x2000), (0x2000, 30, 0x3000),
                                     (0x3000, 21, 0x4000), (0x4000, 12, 0x100000)):
            address = table + 8 * ((gva >> shift) & 511)
            addresses.append(address)
            if large and shift == 21:
                put(memory, address, 0x200087)
                break
            put(memory, address, target | 7)
        return memory, addresses

    def expect(memory, status, code=None, gva=0x123456789ABC, **options):
        nonlocal checks
        result = walk(memory, 0x1000, gva, **{"access": "read", "cpl": 3,
                      "wp": True, "nxe": True, **options})
        assert (result["status"], result["error_code"]) == (status, code), result
        checks += 1
        return result

    memory, entries = fixture()
    original = bytes(memory)
    read = expect(memory, "ok")
    assert read["gpa"] == 0x100ABC and read["leaf_size"] == 4096
    assert [x["entry_gpa"] for x in read["walked"]] == entries
    assert read["ad_required"] == [{"entry_gpa": a, "set_mask": 0x20} for a in entries]
    write = expect(memory, "ok", access="write")
    assert [x["set_mask"] for x in write["ad_required"]] == [0x20, 0x20, 0x20, 0x60]
    assert bytes(memory) == original, "oracle mutated guest memory"
    for index in range(4):
        for flag, access, code in ((2, "write", 7), (4, "read", 5), (1 << 63, "execute", 21)):
            changed = bytearray(memory)
            value = struct.unpack_from("<Q", changed, entries[index])[0]
            put(changed, entries[index], value | flag if flag == 1 << 63 else value & ~flag)
            expect(changed, "pf", code, access=access)
            if flag == 2:
                expect(changed, "ok", access="write", cpl=0, wp=False)
                expect(changed, "pf", 3, access="write", cpl=0, wp=True)
            if flag == 4:
                expect(changed, "ok", cpl=0)
        # P=0 dominates reserved/XD bits in the same entry.
        absent = bytearray(memory)
        put(absent, entries[index], (1 << 63) | (1 << 36))
        expect(absent, "pf", 6, access="write", nxe=False)
        expect(absent, "pf", 20, access="execute", nxe=True)
        expect(absent, "pf", 4, access="execute", nxe=False)
        for flag in (1 << 36, 1 << 51):
            invalid = bytearray(memory)
            put(invalid, entries[index], struct.unpack_from("<Q", invalid, entries[index])[0] | flag)
            expect(invalid, "pf", 13)
        nx = bytearray(memory)
        put(nx, entries[index], struct.unpack_from("<Q", nx, entries[index])[0] | (1 << 63))
        expect(nx, "pf", 13, access="execute", nxe=False)  # RSVD+P+U, I/D=0.
    for index in (0, 1):
        invalid = bytearray(memory)
        put(invalid, entries[index], struct.unpack_from("<Q", invalid, entries[index])[0] | 0x80)
        expect(invalid, "pf", 13)  # PML4 PS or unsupported 1 GiB PDPTE.
    high = 0xFFFF9234567FFFF8
    big, big_entries = fixture(high, large=True)
    translated = expect(big, "ok", gva=high, access="write")
    assert translated["gpa"] == 0x3FFFF8 and translated["leaf_size"] == 0x200000
    assert [x["set_mask"] for x in translated["ad_required"]] == [0x20, 0x20, 0x60]
    for flag in (1 << 13, 1 << 20):
        invalid = bytearray(big)
        put(invalid, big_entries[-1], 0x200087 | flag)
        expect(invalid, "pf", 13, gva=high)
    expect(memory, "gp", 0, gva=0x0000800000000000)
    expect(memory, "gp", 0, gva=0xFFFF7FFFFFFFFFFF)
    present = bytearray(memory)
    put(present, entries[-1], 0)
    expect(present, "pf", 4)
    put(present, entries[-1], 0x100007)
    expect(present, "ok")  # No retained negative translation.
    put(present, entries[-1], 0x100067)
    assert expect(present, "ok", access="write")["ad_required"][-1]["set_mask"] == 0x60
    for flags in (0x18, 0x80):
        attr = bytearray(memory)
        put(attr, entries[-1], 0x100007 | flags)
        expect(attr, "unsupported_attributes")
    software = bytearray(memory)
    for address in entries:
        put(software, address, struct.unpack_from("<Q", software, address)[0] | (0x7FF << 52) | 0xF00)
    expect(software, "ok")  # Ignored software/global/key bits are not reserved.
    unbacked = bytearray(memory)
    put(unbacked, entries[-1], 0x400007)
    assert expect(unbacked, "unsupported_ram")["gpa"] == 0x400ABC
    put(unbacked, entries[0], 0x400007)
    assert expect(unbacked, "unsupported_ram")["gpa"] is None
    for invalid_cr3 in (1 << 36, 1 << 63):
        try:
            walk(memory, invalid_cr3, 0, "read", 0, True, True)
        except ValueError:
            checks += 1
        else:
            raise AssertionError("invalid CR3 context accepted")
    print(f"x86 walk self-check PASS: {checks} synthetic outcomes; no memory mutation or VM")


if __name__ == "__main__":
    self_check()

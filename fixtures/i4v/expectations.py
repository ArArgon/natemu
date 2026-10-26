"""Case-specific x86 expectations, independent of natemu and FEX implementations.

Memory is a sparse {guest_address: byte} dictionary. A prefix stops before the
next instruction, so a fault report can be compared to its committed prefix.
These helpers do not simulate translation, faults, TLBs, or concurrent ordering.
"""

COUNTS = {
    "fex_memory_data64": 1,
    "fex_push_immediates64": 5,
    "fex_moffs_load32": 9,
    "fex_moffs_store32": 8,
}


def expected_after(case_id, registers, memory, completed=None):
    """Return copied registers/memory after a successful instruction prefix.

    Unmodified registers, flags and memory remain exactly as supplied. Legacy
    cases define low-32-bit results only, as recorded in the fixture manifest.
    Missing input bytes raise KeyError rather than silently fabricating RAM.
    """
    count = COUNTS[case_id]
    if completed is None:
        completed = count
    if not isinstance(completed, int) or not 0 <= completed <= count:
        raise ValueError("completed must be an instruction boundary")
    regs, mem = dict(registers), dict(memory)

    def read(address, width):
        return int.from_bytes(bytes(mem[address + i] for i in range(width)), "little")

    def write(address, width, value):
        mem.update(enumerate(value.to_bytes(width, "little"), start=address))

    def set_reg(name, value):
        regs[name] = value

    def push(width, value):
        address = regs["RSP"] - width
        write(address, width, value)
        regs["RSP"] = address

    if case_id == "fex_memory_data64":
        steps = [lambda: set_reg("RAX", read(regs["RBX"], 8))]
    elif case_id == "fex_push_immediates64":
        steps = [
            lambda: push(8, 0xFFFFFFFFBEBDBCBB),
            lambda: push(2, 0x5152),
            lambda: push(2, 0x6162),
            lambda: set_reg("RAX", read(regs["RDX"] - 8, 8)),
            lambda: set_reg("RBX", read(regs["RDX"] - 12, 4)),
        ]
    elif case_id == "fex_moffs_load32":
        steps = [
            lambda: set_reg("RDX", 0x100000),
            lambda: set_reg("RAX", 0x42),
            lambda: write(regs["RDX"], 4, regs["RAX"]),
            lambda: set_reg("RAX", 0xFFFFFFFF),
            lambda: set_reg("RAX", read(0x100000, 4)),
            lambda: set_reg("RDX", regs["RAX"]),
            lambda: set_reg("RAX", 0xFFFFFFFF),
            lambda: set_reg("RAX", (regs["RAX"] & 0xFFFF0000) | read(0x100000, 2)),
            lambda: set_reg("RCX", regs["RAX"]),
        ]
    else:
        steps = [
            lambda: set_reg("RAX", 0x42),
            lambda: write(0x100000, 4, regs["RAX"]),
            lambda: set_reg("RDX", 0x100000),
            lambda: set_reg("RDX", read(regs["RDX"], 4)),
            lambda: set_reg("RAX", 0xFFFF0043),
            lambda: write(0x100000, 2, regs["RAX"] & 0xFFFF),
            lambda: set_reg("RCX", 0x100000),
            lambda: set_reg("RCX", read(regs["RCX"], 4)),
        ]
    for step in steps[:completed]:
        step()
    return regs, mem

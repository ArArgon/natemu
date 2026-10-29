"""Independent reference for MOV-family accesses to a test MMIO device page (64-bit mode), written from the Intel SDM
(Vol. 2 MOV, MOVZX, MOVSX, MOVSXD; Vol. 2A "Instruction Format", ModR/M and SIB tables), not from natemu's code.

`run` executes a straight-line program of MOV-family instructions whose memory operand lies in the device page and
returns the device accesses, the final registers and the device contents. See the task spec in the review ledger for
the device layout: COUNTER at 0xF00..0xF07 (read returns the read count, shifted/masked), SEQUENCE at 0xF08..0xF0F
(read returns the sequence number; write increments it and counts an order error when the value is not the sequence
number), everything else plain little-endian memory. An access that is not wholly inside one register is memory.

AMBIGUITIES (the model's choice):
M1  Several segment prefixes: the last one wins (so 64 then 3E means no base). SDM: ES/CS/SS/DS overrides are ignored
    in 64-bit mode; the effect of a later one on an earlier FS/GS is not stated.
M2  A legacy prefix after REX is not a prefix (REX must be last): it is treated as an opcode and refused.
M3  SEQUENCE write comparison is literal: (value & mask) != (sequence & mask), with no shift by the offset inside
    the register; the tests only write at offset 0xF08.
M4  MOVSXD without REX.W is modelled as a plain MOV r, m of the operand size (SDM: "MOVSXD r32, r/m32" is a move).
"""

M64 = (1 << 64) - 1
PAGE = 4096
COUNTER, SEQ = 0xF00, 0xF08


def _sx(v, bits):
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) else v


def run(code, rip, gpr, fs_base, gs_base, device_linear, device_bytes):
    if len(gpr) != 16 or len(device_bytes) != PAGE or device_linear % PAGE:
        raise ValueError('bad inputs')
    gpr = [g & M64 for g in gpr]
    mem = bytearray(device_bytes)
    st = {'count': 0, 'seq': 0, 'errs': 0}
    accesses = []

    def access(lin, size, write, value=0):
        off = lin - device_linear
        if off < 0 or off + size > PAGE:
            raise ValueError('operand outside device page')
        mask = (1 << 8 * size) - 1
        for base, key in ((COUNTER, 'count'), (SEQ, 'seq')):
            if base <= off and off + size <= base + 8:
                sh = 8 * (off - base)
                if write:
                    value &= mask
                    if key == 'seq':
                        if value != st['seq'] & mask:
                            st['errs'] += 1
                        st['seq'] += 1
                else:
                    value = (st[key] >> sh) & mask
                    if key == 'count':
                        st['count'] += 1
                break
        else:
            if write:
                value &= mask
                mem[off:off + size] = value.to_bytes(size, 'little')
            else:
                value = int.from_bytes(mem[off:off + size], 'little')
        accesses.append((off, size, 1 if write else 0, value))
        return value

    def rd(r, size, rex):
        if size == 1 and not rex and 4 <= r < 8:
            return (gpr[r - 4] >> 8) & 0xFF
        return gpr[r] & ((1 << 8 * size) - 1)

    def wr(r, size, v, rex):
        if size == 8:
            gpr[r] = v & M64
        elif size == 4:
            gpr[r] = v & 0xFFFFFFFF
        elif size == 2:
            gpr[r] = (gpr[r] & ~0xFFFF & M64) | (v & 0xFFFF)
        elif not rex and 4 <= r < 8:
            gpr[r - 4] = (gpr[r - 4] & ~0xFF00 & M64) | ((v & 0xFF) << 8)
        else:
            gpr[r] = (gpr[r] & ~0xFF & M64) | (v & 0xFF)

    pos = 0
    while pos < len(code):
        start = pos

        def fetch(n):
            nonlocal pos
            if pos + n > len(code):
                raise ValueError('truncated instruction %s' % code[start:].hex())
            pos += n
            return int.from_bytes(code[pos - n:pos], 'little')

        def refuse():
            raise ValueError('unsupported instruction %s' % code[start:start + 16].hex())

        opsz16 = False
        seg = 0
        while pos < len(code) and code[pos] in (0x66, 0x26, 0x2E, 0x36, 0x3E, 0x64, 0x65):
            b = fetch(1)
            if b == 0x66:
                opsz16 = True
            else:
                seg = fs_base if b == 0x64 else gs_base if b == 0x65 else 0
        if pos < len(code) and code[pos] in (0xF0, 0xF2, 0xF3, 0x67):
            refuse()
        rex = 0
        if pos < len(code) and 0x40 <= code[pos] <= 0x4F:
            rex = fetch(1)
        W, R, X, B = (rex >> 3) & 1, (rex >> 2) & 1, (rex >> 1) & 1, rex & 1
        osz = 8 if W else 2 if opsz16 else 4
        op = fetch(1)
        if op == 0x0F:
            op = 0x0F00 | fetch(1)

        def modrm():
            """Returns (reg, ea_parts, riprel); refuses mod=3."""
            m = fetch(1)
            mod, reg, rm = m >> 6, ((m >> 3) & 7) | (R << 3), m & 7
            if mod == 3:
                refuse()
            riprel, ea = False, 0
            if rm == 4:
                s = fetch(1)
                sc, ix, bs = s >> 6, ((s >> 3) & 7) | (X << 3), s & 7
                if ix != 4:
                    ea += gpr[ix] << sc
                if bs == 5 and mod == 0:
                    ea += _sx(fetch(4), 32)
                else:
                    ea += gpr[bs | (B << 3)]
            elif rm == 5 and mod == 0:
                riprel = True
                ea += _sx(fetch(4), 32)
            else:
                ea += gpr[rm | (B << 3)]
            if mod == 1:
                ea += _sx(fetch(1), 8)
            elif mod == 2:
                ea += _sx(fetch(4), 32)
            return reg, ea, riprel

        def lin(ea, riprel):
            return ((ea + (rip + pos if riprel else 0)) & M64) + seg & M64

        if op in (0x88, 0x89, 0x8A, 0x8B):
            size = 1 if op in (0x88, 0x8A) else osz
            reg, ea, rr = modrm()
            a = lin(ea, rr)
            if op in (0x88, 0x89):
                access(a, size, 1, rd(reg, size, rex))
            else:
                wr(reg, size, access(a, size, 0), rex)
        elif op in (0xC6, 0xC7):
            size = 1 if op == 0xC6 else osz
            reg, ea, rr = modrm()
            if reg & 7:
                refuse()
            n = min(size, 4)
            imm = fetch(n)
            if size == 8:
                imm = _sx(imm, 32)
            access(lin(ea, rr), size, 1, imm)
        elif 0xA0 <= op <= 0xA3:
            size = 1 if op in (0xA0, 0xA2) else osz
            a = (fetch(8) + seg) & M64
            if op < 0xA2:
                wr(0, size, access(a, size, 0), rex)
            else:
                access(a, size, 1, rd(0, size, rex))
        elif op in (0x0FB6, 0x0FB7, 0x0FBE, 0x0FBF):
            ssz = 1 if op in (0x0FB6, 0x0FBE) else 2
            reg, ea, rr = modrm()
            v = access(lin(ea, rr), ssz, 0)
            if op >= 0x0FBE:
                v = _sx(v, 8 * ssz)
            wr(reg, osz, v, rex)
        elif op == 0x63:
            reg, ea, rr = modrm()
            if W:
                wr(reg, 8, _sx(access(lin(ea, rr), 4, 0), 32), rex)
            else:
                wr(reg, osz, access(lin(ea, rr), osz, 0), rex)
        else:
            refuse()

    return {'accesses': accesses, 'gpr': gpr, 'rip': (rip + len(code)) & M64,
            'device_bytes': bytes(mem), 'order_errors': st['errs']}

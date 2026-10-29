"""Independent reference for the exceptions of the long-mode system instructions natemu executes as resident
transactions, written from the Intel SDM (vol. 2: MOV - Move to/from Control Registers, INVLPG, RDMSR, WRMSR; vol. 3:
2.2.1 EFER, 2.5 control registers, 4.5 CR3 in 4-level paging), not from natemu's code.

Processor profile (natemu's CPU model): read from `models/cpu_profile.json` through `models/cpu_profile.py`, which is the
accepted CPU 16/17 profile (INV-03-01 CLM-03-01-16/-17) with a source on every value. This module no longer holds its own
list: the valid CR0/CR4/EFER bits, the MAXPHYADDR of the CR3 check and the MSR set (index, writable mask, canonical,
memory-type and MISC_ENABLE rules) all come from the profile, so a CR4 bit is valid only if its CPUID feature is
advertised (SDM Vol. 3 2.5). 64-bit mode (CS.L = 1, EFER.LMA = 1) is assumed. x2APIC MSR indices (0x800-0x8ff) belong
to the LAPIC owner and raise ValueError, like the unmodelled 0f 01 forms.

`fault(code, gpr, cpl, cr0, cr4, efer)` -> (vector, error code) the instruction at the start of `code` raises, or None.
Used where neither QEMU nor modes_model gives the answer: pinned QEMU 11.1.2 accepts several of these writes
(reserved CR0 bits 63:32, PE=0 with PG=1, NW without CD, PCIDE without PCID, clearing CR4.PAE, reserved EFER bits,
LME changes with paging on, unknown MSRs) and hangs on others (reserved CR4 bits, SMAP without SMAP, CR3 bits above
MAXPHYADDR, clearing CR0.PG).
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from models import cpu_profile

PROFILE = cpu_profile.load()
UD, GP = (6, 0), (13, 0)
_C = PROFILE.d['control']
CR0 = {n: v['bit'] for n, v in _C['cr0']['bits'].items()}
CR4_ALL = {n: v['bit'] for n, v in _C['cr4']['bits'].items()}
# CR4 bits whose CPUID feature the profile advertises (everything else, PCIDE and SMAP included, is reserved).
CR4_PROFILE = {n: v['bit'] for n, v in _C['cr4']['bits'].items() if v['valid']}
EFER = {n: v['bit'] for n, v in _C['efer']['bits'].items()}
MAXPHYADDR = PROFILE.d['identity']['maxphyaddr']


def bits(names): return sum(1 << b for b in names.values())


def decode(code):
    """(lock, opcode after 0f, modrm or None) of a 64-bit system instruction (legacy prefixes, REX, 0f xx [modrm])."""
    i, lock = 0, False
    while code[i] in (0xF0, 0x66, 0x67, 0x2E, 0x36, 0x3E, 0x26, 0x64, 0x65):
        lock |= code[i] == 0xF0; i += 1
    rex = code[i] if 0x40 <= code[i] <= 0x4F else 0
    i += bool(rex)
    assert code[i] == 0x0F, 'not a two-byte opcode'
    op = code[i + 1]
    modrm = code[i + 2] if op in (0x01, 0x20, 0x22) else None
    return lock, rex, op, modrm


def fault(code, gpr, cpl, cr0, cr4, efer):
    lock, rex, op, modrm = decode(code)
    if lock: return UD                                                   # LOCK on a non-lockable instruction
    if op in (0x20, 0x22):
        cr = (modrm >> 3 & 7) | (8 if rex & 4 else 0)
        if cr not in (0, 2, 3, 4, 8): return UD
    if op == 0x01 and (modrm >> 3 & 7 != 7 or modrm >> 6 == 3):
        raise ValueError('0f 01: only INVLPG m (/7, memory operand) is modelled')
    if cpl != 0: return GP
    if op == 0x22:
        v = gpr[(modrm & 7) | (8 if rex & 1 else 0)]
        if cr == 0:
            if v >> 32: return GP
            if v >> CR0['PG'] & 1 and not v >> CR0['PE'] & 1: return GP
            if v >> CR0['NW'] & 1 and not v >> CR0['CD'] & 1: return GP
            if not v >> CR0['PG'] & 1: return GP                         # clearing PG in 64-bit mode
        if cr == 3 and v >> MAXPHYADDR: return GP                        # bits 63:MAXPHYADDR (PCIDE = 0: bit 63 too)
        if cr == 4:
            if v & ~bits(CR4_PROFILE): return GP
            if (v ^ cr4) & (1 << CR4_ALL['PAE'] | 1 << CR4_ALL['LA57']): return GP   # with EFER.LMA = 1
    if op in (0x30, 0x32):
        idx = gpr[1] & 0xFFFFFFFF
        if op == 0x32:
            if not PROFILE.msr_read_ok(idx): return GP
        else:
            v = (gpr[2] & 0xFFFFFFFF) << 32 | gpr[0] & 0xFFFFFFFF
            if not PROFILE.msr_write_ok(idx, v, cr0=cr0, efer=efer): return GP
    return None

"""The boot-time interpreter's per-instruction cases: the corpus generator and the case format
(docs/restructure/interp-cases.md, which is the format's description; this file is its definition).

    python3 tests/interp/interp_cases.py -o cases.json [--seed N]

Each case is a few instructions from section 1's first-version list, run from a known state in one of the interpreter's
modes (R real mode; P16 the stretch between setting CR0.PE and the far jump, under the stale real-mode CS; P32 protected
mode, paging off, CPL 0; C32 compatibility mode under 4-level paging, CPL 0). A case states the whole architectural
state it starts from (every segment register's hidden part included), its memory, its code and where it stops; the
reference rig (interp_rig.py, interp_stub.asm) runs it on QEMU and under KVM, and natemu's program driver is to set the
same state directly. Deterministic: the same seed gives the same corpus, byte for byte.
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402,E702
import argparse
import hashlib
import json
import random
import subprocess
import tempfile

FORMAT = 'natemu-interp-cases-v1'
SEED = 20261122
HERE = pathlib.Path(__file__).resolve().parent

# --- the machine every case shares: its common blocks (memory every case starts with, besides its own) ---
GDT, GDT_ALT, IDT32, IDT64 = 0x1000, 0x8000, 0x2000, 0x3000
PML4, PML4_ALT = 0x90000, 0x94000
# Descriptors of the case GDT (and of its copy at GDT_ALT). 0x00-0x28 are the stub's own (interp_stub.asm boot_gdt);
# the rest are the cases'. A = accessed (bit 40): every descriptor a starting state was loaded from has it set (the
# stub's own load would set it otherwise); those with it clear are loaded only by a case, which sets it.
DESC = {
    0x00: 0,
    0x08: 0x00CF9B000000FFFF,      # code32 flat
    0x10: 0x00CF93000000FFFF,      # data32 flat
    0x18: 0x00009B007000FFFF,      # code16, base 0x7000 (the stub's 16-bit helper)
    0x20: 0x000093000000FFFF,      # data16, base 0, limit 64 KiB
    0x28: 0x00AF9B000000FFFF,      # code64
    0x30: 0x00CF9A000000FFFF,      # code32 flat, A clear
    0x38: 0x00CF92000000FFFF,      # data32 flat, A clear
    0x40: 0x004093020000FFFF,      # data32, base 0x20000, limit 64 KiB (byte granular)
    0x48: 0x000093030000FFFF,      # data16, base 0x30000, limit 64 KiB
    0x50: 0x00CF9B010000FFFF,      # code32, base 0x10000, limit 4 GiB - 64 KiB
    0x58: 0x00AF9A000000FFFF,      # code64, A clear
    0x60: 0x00CF92040000FFFF,      # data32, base 0x40000, 4 GiB limit (wraps), A clear
}
GDT_LIMIT = len(DESC) * 8 - 1
CR0_PE, CR0_ET, CR0_PG = 0x1, 0x10, 0x80000000
EFER_SCE, EFER_LME, EFER_LMA, EFER_NXE = 0x1, 0x100, 0x400, 0x800
CR4_PAE, CR4_PGE = 0x20, 0x80

# --- every case's layout (linear = physical: paging is off, or C32's tables map identically) ---
CODE = 0x10100          # a case's first instruction
LAND = 0x11000          # a second stop (far transfers, near RET)
DATA = 0x20100          # the data window (64 bytes)
XWIN = 0x30100          # ES's (R, P16) or GS's (P32, C32) window (16 bytes)
FWIN = 0x40100          # FS's window in R/P16 (16 bytes)
GWIN = 0x50100          # GS's window in R/P16 (16 bytes)
STACK = 0x61000         # the stack pointer's linear address
SWIN = (0x60FE0, 0x40)  # the stack window
DEFAULT_WINDOWS = ((DATA, 64), (XWIN, 16), (FWIN, 16), (GWIN, 16), SWIN)
MODES = {               # start state per mode: bits, CR0, CR3, CR4, EFER, IDTR
    'R':   dict(bits=16, cr0=CR0_ET, cr3=0, cr4=0, efer=0, idtr=(0, 0x3ff)),
    'P16': dict(bits=16, cr0=CR0_ET | CR0_PE, cr3=0, cr4=0, efer=0, idtr=(IDT32, 0xff)),
    'P32': dict(bits=32, cr0=CR0_ET | CR0_PE, cr3=0, cr4=0, efer=0, idtr=(IDT32, 0xff)),
    'C32': dict(bits=32, cr0=CR0_PG | CR0_ET | CR0_PE, cr3=PML4, cr4=CR4_PAE, efer=EFER_LME | EFER_LMA, idtr=(IDT64, 0x1ff)),
}
END_MODES = ('R', 'P16', 'P32', 'C32', 'L64')
SREGS = ('es', 'cs', 'ss', 'ds', 'fs', 'gs')
GPRS = ('eax', 'ecx', 'edx', 'ebx', 'esp', 'ebp', 'esi', 'edi')
# Compared bits: EFLAGS less RF and VM (PUSHFD clears them in its image); CR0's defined bits; CR4.VME..OSXMMEXCPT; EFER's
# SCE, LME, LMA, NXE.
EFLAGS_MASK, CR0_MASK, CR4_MASK, EFER_MASK = 0x3C7FD7, 0xE005003F, 0x7FF, 0xD01
CF, PF, AF, ZF, SF, DF, OF, IF = 0x1, 0x4, 0x10, 0x40, 0x80, 0x400, 0x800, 0x200
ARITH = CF | PF | AF | ZF | SF | OF
REFERENCES = ('qemu', 'kvm')
# A far transfer into a code descriptor whose accessed bit is clear sets it (SDM Vol.3 3.4.5.1; AMD does), QEMU 10.0 and 11.1's
# TCG do not (the rig's first runs): the hardware answers those GDT windows.
ACCESSED = 'the GDT window: KVM answers (QEMU does not set a code descriptor\'s accessed bit on a far transfer)'
P16_PROLOGUE = 14       # bytes before a P16 case's first instruction: EAX's slot (4) and the prologue (10)


def real(sel: int, code: bool = False) -> dict:
    """A segment register after a real-mode load (selector and base) over the stub's 16-bit descriptor's limit and
    attributes (code 9BH, data 93H), as it is in real mode and in P16's stale caches."""
    return {'sel': sel, 'base': sel << 4, 'limit': 0xffff, 'attr': 0x9b if code else 0x93}


def loaded(sel: int) -> dict:
    """A segment register loaded in protected mode from DESC[sel] (natemu's Segment::loaded: attributes are descriptor
    bits 47:40 and 55:52, the limit scaled by G), accessed."""
    raw = DESC[sel & ~7]
    limit = raw & 0xffff | (raw >> 32) & 0xf0000
    if raw >> 55 & 1: limit = limit << 12 | 0xfff
    return {'sel': sel, 'base': (raw >> 16) & 0xffffff | (raw >> 56) << 24, 'limit': limit, 'attr': (raw >> 40) & 0xf0ff | 1}


def start_segments(mode: str) -> dict:
    if mode in ('R', 'P16'):
        return {'es': real(0x3000), 'cs': real(0x1000, True), 'ss': real(0x6000), 'ds': real(0x2000),
                'fs': real(0x4000), 'gs': real(0x5000)}
    return {'es': loaded(0x10), 'cs': loaded(0x08), 'ss': loaded(0x10), 'ds': loaded(0x10), 'fs': loaded(0x40), 'gs': loaded(0x48)}


def in_common(a: int) -> bool:
    return any(b <= a < b + len(d) for _, b, d in common_blocks())


@__import__('functools').cache
def common_blocks() -> tuple:
    """(name, address, bytes) of the memory every case starts with: the GDT and its copy, C32's 4-level tables and their
    copy (A and D preset, so no walk writes them; 0-2 MiB in 4 KiB pages, 2 MiB-1 GiB in 2 MiB pages; supervisor)."""
    gdt = b''.join(DESC[s].to_bytes(8, 'little') for s in sorted(DESC))

    def tables(base):
        pml4 = (base + 0x1000 | 0x23).to_bytes(8, 'little') + bytes(4088)
        pdpt = (base + 0x2000 | 0x23).to_bytes(8, 'little') + bytes(4088)
        pd = (base + 0x3000 | 0x23).to_bytes(8, 'little') + b''.join((i << 21 | 0xe3).to_bytes(8, 'little') for i in range(1, 512))
        pt = b''.join((i << 12 | 0x63).to_bytes(8, 'little') for i in range(512))
        return pml4 + pdpt + pd + pt
    return (('gdt', GDT, gdt), ('gdt-alt', GDT_ALT, gdt), ('paging', PML4, tables(PML4)), ('paging-alt', PML4_ALT, tables(PML4_ALT)))


# --- assembling: one nasm run per bitness for the whole corpus ---

def assemble(snippets: list, bits: int) -> list:
    """The bytes of each snippet (nasm source lines), assembled at origin 0: a snippet's code is position independent
    (relative branches inside it, every other address a number), so its bytes are the same wherever a case puts it."""
    src = [f'bits {bits}']
    for i, lines in enumerate(snippets):
        src += [f'dd s{i}e - s{i}', f's{i}:', *lines, f's{i}e:']
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d)
        (p / 'a.asm').write_text('\n'.join(src) + '\n')
        r = subprocess.run(['nasm', '-f', 'bin', '-O9', str(p / 'a.asm'), '-o', str(p / 'a.bin')], capture_output=True, text=True)
        if r.returncode: raise RuntimeError(f'nasm (bits {bits}): {r.stderr[-2000:]}')
        data = (p / 'a.bin').read_bytes()
    out, o = [], 0
    for _ in snippets:
        n = int.from_bytes(data[o:o + 4], 'little'); out.append(data[o + 4:o + 4 + n]); o += 4 + n
    assert o == len(data)
    return out


# --- a case under construction ---

class Case:
    """One case: `asm` (nasm lines) at CODE in `mode`, state overrides, memory, the windows compared, the flags its
    instructions leave undefined, the fields a reference does not answer, extra stops (far transfers, RET)."""

    def __init__(self, mode, group, form, asm, rng, *, regs=None, eflags=None, segs=None, ctl=None, mem=None,
                 windows=(), undefined=0, by=None, stops=(), end=None, note=None, gdtr=None, idtr=None):
        m = MODES[mode]
        self.mode, self.group, self.form, self.asm, self.note = mode, group, form, list(asm), note
        self.bits = m['bits']
        self.segs = start_segments(mode)
        for k, v in (segs or {}).items(): self.segs[k] = v
        self.gpr = [rng.getrandbits(32) for _ in GPRS]
        self.gpr[GPRS.index('esp')] = STACK - self.segs['ss']['base']
        for k, v in (regs or {}).items(): self.gpr[GPRS.index(k)] = v & 0xffffffff
        flags = 0x2 | sum(f for f in (CF, PF, AF, ZF, SF, OF) if rng.random() < 0.5)
        self.eflags = flags if eflags is None else eflags | 0x2
        self.ctl = {k: m[k] for k in ('cr0', 'cr3', 'cr4', 'efer')}
        self.ctl.update(ctl or {})
        self.gdtr = gdtr or (GDT, GDT_LIMIT)
        self.idtr = idtr or m['idtr']
        # windows: (linear, length); every byte of a window starts with random content unless `mem` sets it, but
        # windows over the common blocks (descriptors whose accessed bit a load sets), which start as those are
        self.windows = [*DEFAULT_WINDOWS, *windows]
        self.mem = {}
        for a, n in self.windows:
            self.mem.update({a + i: rng.getrandbits(8) for i in range(n) if not in_common(a + i)})
        for a, b in (mem or {}).items(): self.mem.update({a + i: x for i, x in enumerate(b)})
        self.undefined = undefined
        # `by`: fields a reference does not answer; ('window', linear) names the window that starts there
        self.by = {(f'window{[a for a, _ in self.windows].index(k[1])}' if isinstance(k, tuple) else k): v for k, v in (by or {}).items()}
        # stops: (linear, CS selector, EIP, end mode); the first is the fall-through at the code's end (its linear
        # address is known only once assembled), the others the case's own
        self.end = end or mode
        self.extra_stops = list(stops)

    def cs(self): return self.segs['cs']


def offset(case, seg, linear):
    """The offset of `linear` in segment `seg` of the case's starting state."""
    return linear - case.segs[seg]['base']


def h(v): return f'0x{v:x}'


# --- the generators, per group of section 1's list ---

ALU = ('add', 'or', 'adc', 'sbb', 'and', 'sub', 'xor', 'cmp')
LOGIC = ('and', 'or', 'xor', 'test')
CC = ('o', 'no', 'b', 'ae', 'e', 'ne', 'be', 'a', 's', 'ns', 'p', 'np', 'l', 'ge', 'le', 'g')


def widths(bits):
    """(width, register names by number, the nasm size word) for 8, 16 and 32-bit operands."""
    r8 = ('al', 'cl', 'dl', 'bl', 'ah', 'ch', 'dh', 'bh')
    r16 = ('ax', 'cx', 'dx', 'bx', 'sp', 'bp', 'si', 'di')
    r32 = GPRS
    return ((8, r8, 'byte'), (16, r16, 'word'), (32, r32, 'dword'))


SEG_WINDOW = {           # the window a segment's generated accesses land in, and how much of it they use
    16: {'ds': (DATA, 48), 'es': (XWIN, 16), 'fs': (FWIN, 16), 'gs': (GWIN, 16), 'ss': (SWIN[0] + 0x08, 48)},
    32: {'ds': (DATA, 48), 'es': (XWIN, 16), 'fs': (DATA, 48), 'gs': (XWIN, 16), 'ss': (SWIN[0] + 0x08, 48)},
}
FORMS16 = ('disp', 'bx', 'si', 'di', 'bp', 'bx+si', 'bx+di', 'bp+si', 'bp+di', 'bx+d8', 'si+d16', 'bp+di+d8', 'bx+si+d16')
FORMS32 = ('disp', 'base', 'base+d8', 'base+index*s', 'base+index*s+d32', 'index*s+d32', 'esp+d8')


def mem_operand(mode, rng, seg='ds', width=4, addr32=None):
    """A memory operand in `seg`'s window with a random addressing form: (text, the registers it needs, its linear
    address). 16-bit forms in 16-bit modes unless addr32 (nasm adds the 67 prefix), 32-bit ones (SIB too) in 32-bit
    modes. 16-bit sums wrap at 64 KiB as the processor's do; the upper halves of 16-bit address registers are random
    (ignored). The segment is the form's default or an override (sometimes a redundant one)."""
    bits = MODES[mode]['bits']
    a32 = (bits == 32) if addr32 is None else addr32
    base = start_segments(mode)[seg]['base']
    win, span = SEG_WINDOW[bits][seg]
    target = win + rng.randrange(0, span - width + 1)
    off = target - base
    regs = {}
    if not a32:
        form = rng.choice(FORMS16)
        parts = [p for p in form.split('+') if not p.startswith('d')] if form != 'disp' else []
        disp = (rng.randrange(-0x80, 0x80) if form.endswith('d8') else rng.randrange(0x80, 0x8000) if form.endswith('d16') else 0)
        if not parts: text, default = h(off), 'ds'
        else:
            rest = (off - disp) & 0xffff
            if len(parts) == 2:
                a = rng.randrange(0, 0x10000); regs[parts[0]] = a; regs[parts[1]] = (rest - a) & 0xffff
            else: regs[parts[0]] = rest
            text = '+'.join(parts) + (f'{disp:+#x}' if disp else '')
            default = 'ss' if parts[0] == 'bp' else 'ds'
        regs = {'e' + k: v | rng.getrandbits(16) << 16 for k, v in regs.items()}
        if not parts and bits == 32: text = f'word {text}'            # never reached: 32-bit code takes 32-bit forms
    else:
        form = rng.choice(FORMS32 if seg != 'ss' else FORMS32)
        b = rng.choice(('eax', 'ebx', 'ecx', 'edx', 'esi', 'edi', 'ebp'))
        i = rng.choice([r for r in ('eax', 'ebx', 'ecx', 'edx', 'esi', 'edi', 'ebp') if r != b])
        s = rng.choice((1, 2, 4, 8))
        default = 'ds'
        if form == 'disp': text = f'dword {h(off)}' if bits == 16 else h(off)
        elif form == 'base': regs[b] = off; text = b; default = 'ss' if b == 'ebp' else 'ds'
        elif form == 'base+d8':
            d = rng.randrange(-0x80, 0x80); regs[b] = (off - d) & 0xffffffff; text = f'{b}{d:+#x}'
            default = 'ss' if b == 'ebp' else 'ds'
        elif form == 'base+index*s':
            x = rng.randrange(0, 0x10); regs[i] = x; regs[b] = (off - x * s) & 0xffffffff; text = f'{b}+{i}*{s}'
            default = 'ss' if b == 'ebp' else 'ds'
        elif form == 'base+index*s+d32':
            x = rng.randrange(0, 0x10); d = rng.randrange(0x100, 0x4000); regs[i] = x
            regs[b] = (off - x * s - d) & 0xffffffff; text = f'{b}+{i}*{s}+{h(d)}'
            default = 'ss' if b == 'ebp' else 'ds'
        elif form == 'esp+d8':      # ESP as the base (SS by default): the stack pointer is fixed, the displacement reaches
            sp = STACK - start_segments(mode)['ss']['base']
            d = (target - start_segments(mode)['ss']['base'] - sp) if seg == 'ss' else None
            if d is None or not -0x80 <= d < 0x80:
                regs[b] = off; text = b; default = 'ss' if b == 'ebp' else 'ds'
            else: text = f'esp{d:+#x}'; default = 'ss'
        else:
            # nosplit: nasm would otherwise make ebp*1 (or ebp*2) a base, and SS the default segment
            x = rng.randrange(0, 0x100); regs[i] = x; text = f'nosplit {i}*{s}+{h((off - x * s) & 0xffffffff)}'
    if seg != default or rng.random() < 0.15: text = f'{seg}:{text}'
    return f'[{text}]', regs, target


def gen_mov(mode, rng, out):
    bits = MODES[mode]['bits']
    for w, names, size in widths(bits):
        nb = w // 8
        for _ in range(3):
            a32 = rng.random() < 0.3 if bits == 16 else None
            op, regs, _ = mem_operand(mode, rng, rng.choice(('ds', 'ds', 'es', 'ss', 'fs', 'gs')), nb, addr32=a32)
            r = next(n for n in rng.sample(list(names), len(names)) if n not in ('sp', 'esp') and not any(_overlap(n, x) for x in regs))
            out.append(Case(mode, 'Data movement', f'mov r/m{w}, r{w}', [f'mov {size} {op}, {r}'], rng, regs=regs))
            out.append(Case(mode, 'Data movement', f'mov r{w}, r/m{w}', [f'mov {r}, {size} {op}'], rng, regs=regs))
            out.append(Case(mode, 'Data movement', f'mov r/m{w}, imm{w} (C6/C7)', [f'mov {size} {op}, {h(rng.getrandbits(w))}'], rng, regs=regs))
        a, b = rng.sample(list(names), 2)
        out.append(Case(mode, 'Data movement', f'mov r{w}, r{w}', [f'mov {a}, {b}'], rng))
        out.append(Case(mode, 'Data movement', f'mov r{w}, r{w} (8A/8B form)', [f'db {_modrm_rr(w, a, b, bits)}'], rng))
        out.append(Case(mode, 'Data movement', f'mov r{w}, imm{w} (B0-BF)', [f'mov {rng.choice(names)}, {h(rng.getrandbits(w))}'], rng))
        acc = names[0]          # moffs (A0-A3): the accumulator and an absolute offset, DS or an override
        for seg in ('ds', rng.choice(('es', 'fs', 'gs', 'ss'))):
            win, span = SEG_WINDOW[bits][seg]
            off = win + rng.randrange(0, span - nb + 1) - start_segments(mode)[seg]['base']
            pre = '' if seg == 'ds' else f'{seg}:'
            out.append(Case(mode, 'Data movement', f'mov {acc}, moffs{w} (A0/A1)', [f'mov {acc}, [{pre}{h(off)}]'], rng))
            out.append(Case(mode, 'Data movement', f'mov moffs{w}, {acc} (A2/A3)', [f'mov [{pre}{h(off)}], {acc}'], rng))
        if bits == 16:          # a 67 prefix on the moffs forms: a 32-bit offset
            off = DATA + 8 - start_segments(mode)['ds']['base']
            out.append(Case(mode, 'Data movement', f'mov {acc}, moffs{w} (67 A0/A1)', [f'mov {acc}, [dword {h(off)}]'], rng))


def _overlap(a, b):
    """Whether two register names alias (al/ax/eax, sp/esp ...)."""
    def fam(n):
        n = n[1:] if n.startswith('e') and len(n) == 3 else n
        return {'al': 'a', 'ah': 'a', 'ax': 'a', 'cl': 'c', 'ch': 'c', 'cx': 'c', 'dl': 'd', 'dh': 'd', 'dx': 'd', 'bl': 'b',
                'bh': 'b', 'bx': 'b'}.get(n, n)
    return fam(a) == fam(b)


def _modrm_rr(w, a, b, bits):
    """The 8A/8B (r, r/m) encoding of `mov a, b` (nasm picks 88/89), as a db list."""
    op = 0x8a if w == 8 else 0x8b
    return ', '.join(h(x) for x in _osize(w, bits) + [op, 0xc0 | REGNUM[a] << 3 | REGNUM[b]])


REGNUM = {n: i % 8 for i, n in enumerate(('al', 'cl', 'dl', 'bl', 'ah', 'ch', 'dh', 'bh', 'ax', 'cx', 'dx', 'bx', 'sp', 'bp', 'si', 'di',
                                          *GPRS))}


def _osize(w, bits):
    return [0x66] if (w == 32 and bits == 16) or (w == 16 and bits == 32) else []


def gen_movzx_lea(mode, rng, out):
    bits = MODES[mode]['bits']
    for dst in (('ax', 'cx', 'si'), ('eax', 'edx', 'edi')):
        for src_w, size in ((8, 'byte'), (16, 'word')):
            if dst[0] == 'ax' and src_w == 16: continue
            op, regs, _ = mem_operand(mode, rng, rng.choice(('ds', 'es', 'ss')), src_w // 8)
            d = next((x for x in rng.sample(dst, 3) if not any(_overlap(x, y) for y in regs)), None)
            if d is None: continue
            dw = 16 if len(d) == 2 else 32
            out.append(Case(mode, 'Data movement', f'movzx r{dw}, m{src_w}', [f'movzx {d}, {size} {op}'], rng, regs=regs))
            s = rng.choice(('bl', 'ch', 'dl') if src_w == 8 else ('bx', 'cx', 'si'))
            out.append(Case(mode, 'Data movement', f'movzx r{dw}, r{src_w}', [f'movzx {d}, {s}'], rng))
    for d in ('ax', 'si', 'eax', 'ebp'):
        for a32 in (False, True):
            if a32 or bits == 16:
                op, regs, _ = mem_operand(mode, rng, 'ds', 1, addr32=a32)
                op = op.replace('ds:', '').replace('ss:', '').replace('es:', '').replace('fs:', '').replace('gs:', '')
            else:               # a 16-bit address in 32-bit code (67): LEA only computes it
                regs = {'ebx': rng.getrandbits(32), 'esi': rng.getrandbits(32)}
                op = f'[bx+si+{h(rng.randrange(0x10, 0x7f))}]'
            out.append(Case(mode, 'Data movement', f'lea r{16 if len(d) == 2 else 32}, m (a{32 if a32 else 16})',
                            [f'lea {d}, {op}'], rng, regs=regs))


def gen_alu(mode, rng, out):
    bits = MODES[mode]['bits']
    for op in ALU:
        und = 0 if op not in LOGIC else AF
        for w, names, size in widths(bits):
            a, b = rng.sample([n for n in names if n not in ('sp', 'esp')], 2)
            # register forms (00-03), both directions
            out.append(Case(mode, 'Arithmetic and logic', f'{op} r{w}, r{w}', [f'{op} {a}, {b}'], rng, undefined=und))
            if w == bits:
                out.append(Case(mode, 'Arithmetic and logic', f'{op} r{w}, r{w} (02/03 form)',
                                [f'db {_alu_rr_rev(op, w, a, b, bits)}'], rng, undefined=und))
            # memory forms
            m, regs, _ = mem_operand(mode, rng, rng.choice(('ds', 'ds', 'es', 'ss', 'fs', 'gs')), w // 8,
                                     addr32=(rng.random() < 0.25) if bits == 16 else None)
            r = next(n for n in rng.sample(list(names), len(names)) if n not in ('sp', 'esp') and not any(_overlap(n, x) for x in regs))
            pre = ''
            out.append(Case(mode, 'Arithmetic and logic', f'{op} m{w}, r{w}', [f'{pre}{op} {size} {m}, {r}'], rng, regs=regs, undefined=und))
            out.append(Case(mode, 'Arithmetic and logic', f'{op} r{w}, m{w}', [f'{pre}{op} {r}, {size} {m}'], rng, regs=regs, undefined=und))
            # immediates: the accumulator form (04/05), 80/81, 83 (sign-extended imm8)
            out.append(Case(mode, 'Arithmetic and logic', f'{op} acc{w}, imm (04/05)', [f'{op} {names[0]}, {h(rng.getrandbits(w))}'], rng, undefined=und))
            reg = rng.choice([n for n in names[1:] if n not in ('sp', 'esp')])
            out.append(Case(mode, 'Arithmetic and logic', f'{op} r/m{w}, imm{w} (80/81)', [f'{op} {reg}, strict {size} {h(rng.getrandbits(w))}'], rng, undefined=und))
            if w > 8:
                out.append(Case(mode, 'Arithmetic and logic', f'{op} m{w}, imm8 (83)', [f'{pre}{op} {size} {m}, strict byte {rng.randrange(-128, 128)}'], rng, regs=regs, undefined=und))
            else:
                out.append(Case(mode, 'Arithmetic and logic', f'{op} m{w}, imm (80)', [f'{pre}{op} {size} {m}, strict {size} {h(rng.getrandbits(w))}'], rng, regs=regs, undefined=und))
    # carry chains: ADC/SBB with CF set and clear, edge values
    for op in ('adc', 'sbb', 'add', 'sub'):
        for cf in (0, CF):
            v = rng.choice((0xffffffff, 0x7fffffff, 0x80000000, 0))
            out.append(Case(mode, 'Arithmetic and logic', f'{op} edge (CF={cf})', [f'{op} eax, ebx'], rng,
                            regs={'eax': v, 'ebx': rng.choice((1, 0xffffffff, 0x80000000))}, eflags=cf))


def _alu_rr_rev(op, w, a, b, bits):
    """The 02/03 (r, r/m) encoding of `op a, b` (nasm picks 00/01), as a db list."""
    return ', '.join(h(x) for x in _osize(w, bits) + [ALU.index(op) * 8 + (2 if w == 8 else 3), 0xc0 | REGNUM[a] << 3 | REGNUM[b]])


def gen_test_incdec_shift(mode, rng, out):
    bits = MODES[mode]['bits']
    for w, names, size in widths(bits):
        a, b = rng.sample([n for n in names if n not in ('sp', 'esp')], 2)
        out.append(Case(mode, 'Arithmetic and logic', f'test r{w}, r{w}', [f'test {a}, {b}'], rng, undefined=AF))
        out.append(Case(mode, 'Arithmetic and logic', f'test acc{w}, imm (A8/A9)', [f'test {names[0]}, {h(rng.getrandbits(w))}'], rng, undefined=AF))
        out.append(Case(mode, 'Arithmetic and logic', f'test r/m{w}, imm (F6/F7)', [f'test {b}, {h(rng.getrandbits(w))}'], rng, undefined=AF))
        m, regs, _ = mem_operand(mode, rng, rng.choice(('ds', 'ss', 'es')), w // 8)
        r = next(n for n in names if n not in ('sp', 'esp') and not any(_overlap(n, x) for x in regs))
        pre = ''
        out.append(Case(mode, 'Arithmetic and logic', f'test m{w}, r{w}', [f'{pre}test {size} {m}, {r}'], rng, regs=regs, undefined=AF))
        for op in ('inc', 'dec'):
            reg = rng.choice([n for n in names if n not in ('sp', 'esp')])
            v = rng.choice((0x7f, 0x80, 0xff, 0x7fff, 0xffff, 0x7fffffff, 0xffffffff, 0, rng.getrandbits(32)))
            out.append(Case(mode, 'Arithmetic and logic', f'{op} r{w} ({"40-4F" if w > 8 else "FE"})', [f'{op} {reg}'], rng,
                            regs={_full(reg): v}))
            out.append(Case(mode, 'Arithmetic and logic', f'{op} m{w}', [f'{pre}{op} {size} {m}'], rng, regs=regs))
            if w > 8:
                out.append(Case(mode, 'Arithmetic and logic', f'{op} r{w} (FF /{0 if op == "inc" else 1})',
                                [f'db {_incdec_ff(op, w, reg, bits)}'], rng, regs={_full(reg): v}))
        for op in ('shl', 'shr', 'sar'):
            reg = rng.choice([n for n in names if n not in ('sp', 'esp', 'cl', 'cx', 'ecx')])
            for count in (1, 0, rng.randrange(2, w - 1), w - 1, rng.randrange(w, 32) if w < 32 else 33):
                c = count & 31
                und = 0 if c == 0 else AF | (OF if c > 1 else 0) | (CF if c >= w and op != 'sar' else 0)
                if c >= w and op == 'sar': und |= CF if w < 32 else 0
                form = 'D0/D1' if count == 1 else 'C0/C1'
                text = f'{op} {reg}, 1' if count == 1 else f'{op} {reg}, {count}'
                out.append(Case(mode, 'Arithmetic and logic', f'{op} r{w}, {form}', [text], rng, undefined=und))
                if count in (0, w - 1, 33):
                    out.append(Case(mode, 'Arithmetic and logic', f'{op} r{w}, cl (D2/D3)', [f'{op} {reg}, cl'], rng,
                                    regs={'ecx': count | rng.getrandbits(24) << 8}, undefined=und))
            count = rng.randrange(1, 8)
            und = AF | (OF if count > 1 else 0)
            out.append(Case(mode, 'Arithmetic and logic', f'{op} m{w}, imm8', [f'{pre}{op} {size} {m}, {count}'], rng, regs=regs, undefined=und))


def _full(reg):
    return {'al': 'eax', 'cl': 'ecx', 'dl': 'edx', 'bl': 'ebx', 'ah': 'eax', 'ch': 'ecx', 'dh': 'edx', 'bh': 'ebx'}.get(reg) or \
        (reg if len(reg) == 3 else 'e' + reg)


def _incdec_ff(op, w, reg, bits):
    return ', '.join(h(x) for x in _osize(w, bits) + [0xff, 0xc0 | (0 if op == 'inc' else 1) << 3 | REGNUM[reg]])


def mem_operand_base(mode, seg):
    return start_segments(mode)[seg]['base']


def gen_bits(mode, rng, out):
    bits = MODES[mode]['bits']
    und = OF | SF | AF | PF
    for op in ('bt', 'bts', 'btr', 'btc'):
        for w, names, size in widths(bits)[1:]:
            reg, idx = rng.sample([n for n in names if n not in ('sp', 'esp')], 2)
            out.append(Case(mode, 'Bit operations', f'{op} r{w}, r{w}', [f'{op} {reg}, {idx}'], rng, undefined=und))
            out.append(Case(mode, 'Bit operations', f'{op} r{w}, imm8', [f'{op} {reg}, {rng.randrange(0, 256)}'], rng, undefined=und))
            # the memory bit string: a register offset reaches bytes beyond the operand (negative too); the base sits mid-window
            base_lin = DATA + 24
            off = base_lin - mem_operand_base(mode, 'ds')
            b = 'bx' if bits == 16 else 'ebx'
            bitoff = rng.randrange(-64, 128)
            r = 'si' if w == 16 else 'esi'
            out.append(Case(mode, 'Bit operations', f'{op} m{w}, r{w} (bit string)', [f'{op} {size} [{b}], {r}'], rng,
                            regs={'ebx': off, 'esi': bitoff & 0xffffffff if w == 32 else (bitoff & 0xffff) | rng.getrandbits(16) << 16},
                            undefined=und))
            out.append(Case(mode, 'Bit operations', f'{op} m{w}, imm8', [f'{op} {size} [{b}], {rng.randrange(0, 256)}'], rng,
                            regs={'ebx': off}, undefined=und))
    # LOCK forms (Linux's trampoline lock, kvm-unit-tests' stack claim)
    off = DATA + 16 - mem_operand_base(mode, 'ds')
    b = 'bx' if bits == 16 else 'ebx'
    for w, size, r in ((16, 'word', 'cx'), (32, 'dword', 'ecx')):
        out.append(Case(mode, 'Atomics', f'lock bts m{w}, imm8', [f'lock bts {size} [{b}], {rng.randrange(0, w)}'], rng, regs={'ebx': off}, undefined=und))
        out.append(Case(mode, 'Atomics', f'lock bts m{w}, r{w}', [f'lock bts {size} [{b}], {r}'], rng,
                        regs={'ebx': off, 'ecx': rng.randrange(0, 64)}, undefined=und))
        out.append(Case(mode, 'Atomics', f'lock xadd m{w}, r{w}', [f'lock xadd {size} [{b}], {r}'], rng, regs={'ebx': off}))
    out.append(Case(mode, 'Atomics', 'lock xadd m8, r8', [f'lock xadd byte [{b}], cl'], rng, regs={'ebx': off}))


def gen_flow(mode, rng, out):
    bits = MODES[mode]['bits']
    ip = 'ip' if bits == 16 else 'eip'
    for cc in CC:
        for flags in (rng.getrandbits(12) & ARITH, rng.getrandbits(12) & ARITH):
            out.append(Case(mode, 'Control flow', f'j{cc} rel8', [f'j{cc} short .t', 'inc ecx', '.t:'], rng, eflags=flags))
        out.append(Case(mode, 'Control flow', f'j{cc} rel{bits}', [f'j{cc} near .t', 'inc ecx', '.t:'], rng,
                        eflags=rng.getrandbits(12) & ARITH))
    out.append(Case(mode, 'Control flow', 'jmp rel8', ['jmp short .t', 'inc ecx', '.t:'], rng))
    out.append(Case(mode, 'Control flow', f'jmp rel{bits}', ['jmp near .t', 'inc ecx', '.t:'], rng))
    if bits == 16:
        out.append(Case(mode, 'Control flow', 'jmp rel32 (66)', ['o32 jmp near .t', 'inc ecx', '.t:'], rng))
        out.append(Case(mode, 'Control flow', 'jnz rel32 (66)', ['o32 jnz near .t', 'inc ecx', '.t:'], rng, eflags=0))
    for count in (1, 2, 0x10001, 0):
        reg = 'ecx'
        out.append(Case(mode, 'Control flow', f'loop rel8 ({"cx" if bits == 16 else "ecx"})', ['loop .t', 'inc edx', '.t:'], rng,
                        regs={reg: count | (rng.getrandbits(15) << 17 if bits == 16 else 0)}))
    out.append(Case(mode, 'Control flow', f'loop rel8 ({"ecx" if bits == 16 else "cx"}, 67)',
                    [f'{"a32" if bits == 16 else "a16"} loop .t', 'inc edx', '.t:'], rng, regs={'ecx': 0x10000 if bits == 16 else 0x30001}))
    # near CALL pushes the return IP; near RET pops one (to the second stop)
    out.append(Case(mode, 'Control flow', f'call rel{bits}', ['call .t', 'inc ecx', '.t:'], rng))
    if bits == 16:
        out.append(Case(mode, 'Control flow', 'call rel32 (66)', ['o32 call .t', 'inc ecx', '.t:'], rng))
    cs = start_segments(mode)['cs']
    land_ip = LAND - cs['base']
    w = 2 if bits == 16 else 4
    sp_lin = STACK
    for o in ('', 'o32 ' if bits == 16 else 'o16 '):
        ww = (4 if o else 2) if bits == 16 else (2 if o else 4)
        if bits == 32 and o: continue          # a 16-bit near RET in 32-bit code truncates EIP: not a boot path
        out.append(Case(mode, 'Control flow', f'ret{" (" + o.strip() + ")" if o else ""}', [f'{o}ret'], rng,
                        mem={sp_lin: land_ip.to_bytes(ww, 'little')}, stops=[(LAND, cs['sel'], land_ip, mode)]))


def gen_far(mode, rng, out):
    """Far JMP ptr16:16, ptr16:32, m16:32 and 32-bit far RET, into the modes section 1 names."""
    bits = MODES[mode]['bits']
    data_off = DATA + 8 - mem_operand_base(mode, 'ds')
    targets = {                            # (selector, EIP at LAND, end mode)
        'R': [(0x1100, LAND - 0x11000, 'R'), (0x0f00, LAND - 0xf000, 'R')],
        'P16': [(0x08, LAND, 'P32'), (0x30, LAND, 'P32'), (0x50, LAND - 0x10000, 'P32')],
        'P32': [(0x08, LAND, 'P32'), (0x30, LAND, 'P32'), (0x50, LAND - 0x10000, 'P32')],
        'C32': [(0x08, LAND, 'C32'), (0x28, LAND, 'L64'), (0x58, LAND, 'L64'), (0x30, LAND, 'C32'), (0x50, LAND - 0x10000, 'C32')],
    }[mode]
    gdt_win = [(GDT + 0x30, 8), (GDT + 0x58, 8)]
    by, note = {('window', GDT + 0x30): 'kvm', ('window', GDT + 0x58): 'kvm'}, ACCESSED
    for sel, eip, end in targets:
        stop = [(LAND, sel, eip, end)]
        if eip < 0x10000:
            out.append(Case(mode, 'Far transfers', 'jmp far ptr16:16', [f'{"o16 " if bits == 32 else ""}jmp {h(sel)}:{h(eip)}'], rng,
                            stops=stop, windows=gdt_win, by=by, note=note))
        out.append(Case(mode, 'Far transfers', 'jmp far ptr16:32', [f'jmp dword {h(sel)}:{h(eip)}'], rng, stops=stop, windows=gdt_win,
                        by=by, note=note))
        ptr = eip.to_bytes(4, 'little') + sel.to_bytes(2, 'little')
        b = 'bx' if bits == 16 else 'ebx'
        out.append(Case(mode, 'Far transfers', 'jmp far m16:32', [f'jmp far dword [{b}]'], rng, regs={'ebx': data_off},
                        mem={DATA + 8: ptr}, stops=stop, windows=gdt_win, by=by, note=note))
        # far RET, 32-bit operand size: EIP then CS in a doubleword
        out.append(Case(mode, 'Far transfers', 'retf (32-bit)', ['o32 retf'], rng,
                        mem={STACK: eip.to_bytes(4, 'little') + (sel | rng.getrandbits(16) << 16).to_bytes(4, 'little')},
                        stops=stop, windows=gdt_win, by=by, note=note))


def gen_stack(mode, rng, out):
    bits = MODES[mode]['bits']
    for w, names, size in widths(bits)[1:]:
        for r in rng.sample(list(names), 4) + [names[4]]:
            out.append(Case(mode, 'Stack', f'push r{w}', [f'push {r}'], rng))
            out.append(Case(mode, 'Stack', f'pop r{w}', [f'pop {r}'], rng))
        out.append(Case(mode, 'Stack', f'push imm{w} (68)', [f'push {size} {h(rng.getrandbits(w))}'], rng))
        out.append(Case(mode, 'Stack', f'push imm8 (6A, o{w})', [f'push {size} strict byte {rng.randrange(-128, 128)}'], rng))
        out.append(Case(mode, 'Stack', f'push/pop r{w} pair', [f'push {names[3]}', f'push {size} {h(rng.getrandbits(w))}', f'pop {names[6]}', f'pop {names[1]}'], rng))
        o = '' if (w == 16) == (bits == 16) else ('o32 ' if bits == 16 else 'o16 ')
        out.append(Case(mode, 'Stack', f'pushf{"d" if w == 32 else ""}', [f'{o}pushf'], rng,
                        eflags=rng.getrandbits(12) & (ARITH | DF) | rng.choice((0, IF))))
        for v in (rng.getrandbits(32) & 0x244cd5 | 0x2, 0x3000 | ARITH, 0x40000 | 0x200000 | 0x4000):
            if w == 16: v &= 0xffff
            out.append(Case(mode, 'Stack', f'popf{"d" if w == 32 else ""}', [f'{o}popf'], rng,
                            mem={STACK: v.to_bytes(4, 'little')}))


def gen_segments(mode, rng, out):
    bits = MODES[mode]['bits']
    o32 = 'o32 ' if bits == 16 else ''
    if mode == 'R':
        for sreg in ('ds', 'es', 'fs', 'gs', 'ss'):
            for _ in range(2):
                sel = rng.randrange(0x2000, 0x8000)
                lin = (sel << 4) + 0x10
                via = 'ss:' if sreg == 'ss' else f'{sreg}:'
                out.append(Case(mode, 'Segments and tables', f'mov {sreg}, r16', [f'mov {sreg}, ax', f'mov [{via}0x10], ecx'], rng,
                                regs={'eax': sel | rng.getrandbits(16) << 16}, windows=[(lin, 8)]))
            sel = rng.randrange(0x2000, 0x8000)
            off = DATA + 4 - 0x20000
            out.append(Case(mode, 'Segments and tables', f'mov {sreg}, m16', [f'mov {sreg}, [{h(off)}]', f'mov [{sreg}:0x20], ebx'], rng,
                            mem={DATA + 4: sel.to_bytes(2, 'little')}, windows=[((sel << 4) + 0x20, 8)]))
    else:
        sels = {'ds': (0x10, 0x38, 0x40, 0x60, 0x48), 'es': (0x10, 0x40, 0x38), 'fs': (0x10, 0x60), 'gs': (0x48, 0x38),
                'ss': (0x10, 0x38, 0x60)}
        gwin = [(GDT + 0x38, 8), (GDT + 0x60, 8)]
        for sreg, choices in sels.items():
            for sel in choices:
                d = loaded(sel)
                lin = (d['base'] + 0x20100) & 0xffffffff if d['limit'] > 0xfffff else d['base'] + 0x100
                if sreg == 'ss': lin = STACK            # a new stack pointer for the new SS, at the same linear address
                off = lin - d['base'] & 0xffffffff
                if sreg == 'ss':
                    body = ['mov ss, ax', f'mov esp, {h(off)}', 'push ecx']
                    win = []
                else:
                    body = [f'mov {sreg}, ax', f'{"a32 " if bits == 16 and off > 0xffff else ""}mov [{sreg}:{h(off)}], ecx']
                    if bits == 16 and off > 0xffff: continue
                    win = [(lin, 8)]
                out.append(Case(mode, 'Segments and tables', f'mov {sreg}, r16', body, rng, regs={'eax': sel | rng.getrandbits(16) << 16},
                                windows=win + gwin))
        out.append(Case(mode, 'Segments and tables', 'mov ds, r16 (null)', ['mov ds, ax'], rng, regs={'eax': rng.randrange(0, 4)}))
        out.append(Case(mode, 'Segments and tables', 'mov es, m16', [f'mov es, [{h(DATA + 4)}]' if bits == 32 else f'mov es, [{h(DATA + 4 - 0x20000)}]'], rng,
                        mem={DATA + 4: (0x40).to_bytes(2, 'little')}))
    for sreg in SREGS:
        out.append(Case(mode, 'Segments and tables', f'mov r16, {sreg}', [f'mov ax, {sreg}'], rng))
        out.append(Case(mode, 'Segments and tables', f'mov r32, {sreg}', [f'mov edx, {sreg}'], rng))
        off = DATA + 6 - mem_operand_base(mode, 'ds')
        out.append(Case(mode, 'Segments and tables', f'mov m16, {sreg}', [f'{o32}mov [{h(off)}], {sreg}'], rng))
    # LGDT/LIDT: 16-bit operand size loads a 24-bit base, 32-bit a 32-bit one; the GDTs loaded keep the stub's descriptors
    off = DATA + 0x10 - mem_operand_base(mode, 'ds')
    for opsize in (16, 32):
        pre = ('o32 ' if opsize == 32 else '') if bits == 16 else ('o16 ' if opsize == 16 else '')
        for base, limit in ((GDT_ALT, GDT_LIMIT), (0xab000000 | GDT_ALT, 0x3f), (GDT, GDT_LIMIT)):
            if opsize == 32 and base > 0xffffff: base = GDT_ALT
            out.append(Case(mode, 'Segments and tables', f'lgdt m (o{opsize})', [f'{pre}lgdt [{h(off)}]'], rng,
                            mem={DATA + 0x10: limit.to_bytes(2, 'little') + base.to_bytes(4, 'little')}))
        idt = MODES[mode]['idtr']
        for base, limit in ((idt[0], idt[1]), (0x12345678, 0x7ff)):
            out.append(Case(mode, 'Segments and tables', f'lidt m (o{opsize})', [f'{pre}lidt [{h(off)}]'], rng,
                            mem={DATA + 0x10: limit.to_bytes(2, 'little') + base.to_bytes(4, 'little')}))


def gen_control(mode, rng, out):
    bits = MODES[mode]['bits']
    und = ARITH          # MOV to and from control registers leaves the arithmetic flags undefined
    base = MODES[mode]
    for cr in ('cr0', 'cr3', 'cr4'):
        out.append(Case(mode, 'Control registers, MSRs', f'mov r32, {cr}', [f'mov ebx, {cr}'], rng, undefined=und))
    # CR0 bits that change no mode (MP, EM, TS, NE, WP, AM), set and cleared
    for bitsv in (0x2, 0x4, 0x8, 0x20, 0x10000, 0x40000, 0x2e):
        v = base['cr0'] | bitsv
        out.append(Case(mode, 'Control registers, MSRs', 'mov cr0, r32 (flags)', ['mov cr0, eax'], rng, regs={'eax': v}, undefined=und))
        out.append(Case(mode, 'Control registers, MSRs', 'mov cr0, r32 (clear)', ['mov cr0, eax'], rng, regs={'eax': base['cr0']},
                        ctl={'cr0': v}, undefined=und))
    if mode in ('R', 'P16', 'P32'):
        for v in (0x12345000, 0x9a000 | 0x18, PML4):
            out.append(Case(mode, 'Control registers, MSRs', 'mov cr3, r32', ['mov cr3, edx'], rng, regs={'edx': v}, undefined=und))
        # (PSE PCE OSFXSR OSXMMEXCPT: features natemu's CPU profile has; MCE, which it does not advertise, is #GP there.)
        for v in (CR4_PAE, CR4_PAE | CR4_PGE, 0x10 | 0x100 | 0x200 | 0x400, 0x8):
            out.append(Case(mode, 'Control registers, MSRs', 'mov cr4, r32', ['mov cr4, ecx'], rng, regs={'ecx': v}, undefined=und))
    else:      # C32: the alternate tables, PGE on and off; PAE stays
        out.append(Case(mode, 'Control registers, MSRs', 'mov cr3, r32', ['mov cr3, edx', f'mov [{h(DATA)}], ebx'], rng,
                        regs={'edx': PML4_ALT}, undefined=und))
        out.append(Case(mode, 'Control registers, MSRs', 'mov cr3, r32 (same)', ['mov cr3, edx'], rng, regs={'edx': PML4}, undefined=und))
        for v in (CR4_PAE | CR4_PGE, CR4_PAE | 0x200 | 0x400, CR4_PAE | 0x10):
            out.append(Case(mode, 'Control registers, MSRs', 'mov cr4, r32', ['mov cr4, ecx', f'mov [{h(DATA)}], ebx'], rng,
                            regs={'ecx': v}, undefined=und))
    # mode transitions (section 6), each ending in the mode it enters
    if mode == 'R':
        out.append(Case(mode, 'Control registers, MSRs', 'mov cr0, r32 (set PE: R -> P16)', ['mov cr0, eax'], rng,
                        regs={'eax': CR0_ET | CR0_PE}, undefined=und, end='P16'))
        out.append(Case(mode, 'Control registers, MSRs', 'mov cr0 (PE), jmp far ptr16:32 (R -> P32)',
                        ['mov eax, cr0', 'or al, 1', 'mov cr0, eax', f'jmp dword 0x08:{h(LAND)}'], rng, undefined=und,
                        stops=[(LAND, 0x08, LAND, 'P32')]))
        out.append(Case(mode, 'Control registers, MSRs', 'o32 lgdt, mov cr0 (PE), jmp far ptr16:32 (R -> P32)',
                        [f'o32 lgdt [{h(DATA + 0x10 - 0x20000)}]', 'mov eax, cr0', 'or al, 1', 'mov cr0, eax', f'jmp dword 0x30:{h(LAND)}'],
                        rng, undefined=und, mem={DATA + 0x10: GDT_LIMIT.to_bytes(2, 'little') + GDT_ALT.to_bytes(4, 'little')},
                        stops=[(LAND, 0x30, LAND, 'P32')], windows=[(GDT_ALT + 0x30, 8)], by={('window', GDT_ALT + 0x30): 'kvm'},
                        note=ACCESSED))
    if mode == 'P16':
        out.append(Case(mode, 'Control registers, MSRs', 'mov cr0, r32 (clear PE: P16 -> R)', ['mov cr0, eax'], rng,
                        regs={'eax': CR0_ET}, undefined=und, end='R'))
    if mode == 'P32':
        tables = {'cr3': PML4, 'cr4': CR4_PAE, 'efer': EFER_LME}
        out.append(Case(mode, 'Control registers, MSRs', 'mov cr0, r32 (set PG with LME: P32 -> C32)', ['mov cr0, eax'], rng,
                        regs={'eax': CR0_PG | CR0_ET | CR0_PE}, ctl=tables, undefined=und, end='C32'))
        out.append(Case(mode, 'Control registers, MSRs', 'mov cr0 (PG), jmp far to 64-bit (P32 -> L64)',
                        ['mov cr0, eax', f'jmp 0x28:{h(LAND)}'], rng, regs={'eax': CR0_PG | CR0_ET | CR0_PE}, ctl=tables,
                        undefined=und, stops=[(LAND, 0x28, LAND, 'L64')]))
    if mode == 'C32':
        out.append(Case(mode, 'Control registers, MSRs', 'mov cr0, r32 (clear PG: C32 -> P32)', ['mov cr0, eax'], rng,
                        regs={'eax': CR0_ET | CR0_PE}, undefined=und, end='P32'))
    # MSRs: EFER (read, and the bits a boot sets), IA32_MISC_ENABLE in real mode (its value is the CPU's: natemu's profile
    # answers it), IA32_GS_BASE in 32-bit modes (and a GS access through the base it sets)
    out.append(Case(mode, 'Control registers, MSRs', 'rdmsr (EFER)', ['rdmsr'], rng, regs={'ecx': 0xC0000080}))
    for v in ((EFER_SCE, EFER_NXE, EFER_LME, EFER_SCE | EFER_NXE) if mode != 'C32' else (EFER_LME | EFER_NXE, EFER_LME | EFER_SCE)):
        out.append(Case(mode, 'Control registers, MSRs', 'wrmsr (EFER)', ['wrmsr'], rng, regs={'ecx': 0xC0000080, 'eax': v, 'edx': 0}))
    if mode == 'R':
        out.append(Case(mode, 'Control registers, MSRs', 'rdmsr (IA32_MISC_ENABLE)', ['rdmsr'], rng, regs={'ecx': 0x1a0},
                        by={'eax': 'profile', 'edx': 'profile'}))
    if mode in ('P32', 'C32'):
        gs = 0x45000 + rng.randrange(0, 0x100) * 16
        out.append(Case(mode, 'Control registers, MSRs', 'wrmsr (IA32_GS_BASE), rdmsr',
                        ['wrmsr', 'xor eax, eax', 'xor edx, edx', 'rdmsr', 'mov [gs:0x10], esi'], rng,
                        regs={'ecx': 0xC0000101, 'eax': gs, 'edx': 0}, windows=[(gs + 0x10, 4)], undefined=AF))
    # CPUID: the outputs are the CPU's (natemu's profile answers them); the rest of the state is compared
    for leaf in (0, 1, 0x80000000, 0x80000001, 7):
        out.append(Case(mode, 'Control registers, MSRs', f'cpuid (leaf {h(leaf)})', ['cpuid'], rng, regs={'eax': leaf, 'ecx': 0},
                        by={r: 'profile' for r in ('eax', 'ebx', 'ecx', 'edx')}))


def gen_other(mode, rng, out):
    bits = MODES[mode]['bits']
    out.append(Case(mode, 'Other', 'cli', ['cli'], rng, eflags=IF | (rng.getrandbits(12) & ARITH)))
    out.append(Case(mode, 'Other', 'cli (IF already clear)', ['cli'], rng))
    out.append(Case(mode, 'Other', 'cld', ['cld'], rng, eflags=DF | (rng.getrandbits(12) & ARITH)))
    out.append(Case(mode, 'Other', 'pause', ['pause'], rng))
    out.append(Case(mode, 'Other', 'wbinvd', ['wbinvd'], rng))
    out.append(Case(mode, 'Other', 'nop', ['nop'], rng))
    b = 'bx' if bits == 16 else 'ebx'
    out.append(Case(mode, 'Other', 'nop r/m (0F 1F)', [f'nop word [{b}+0x10]', f'nop dword [{b}+0x10]', 'db 0x0f, 0x1f, 0x00'], rng,
                    regs={'ebx': rng.getrandbits(16), 'eax': rng.getrandbits(16)}))
    multi = (['db 0x66, 0x0f, 0x1f, 0x44, 0x00', 'db 0x0f, 0x1f, 0x80, 0, 0'] if bits == 16 else      # [si+0], [bx+si+0]
             ['db 0x66, 0x0f, 0x1f, 0x44, 0x00, 0x00', 'db 0x0f, 0x1f, 0x80, 0, 0, 0, 0'])                # [eax+eax+0], [eax+0]
    out.append(Case(mode, 'Other', 'nop (multi-byte)', multi, rng,
                    regs={'eax': rng.getrandbits(16)}))
    # prefixes: a CS override on a load, redundant and repeated segment prefixes, 66 and 67 together
    off = DATA + 0x20 - mem_operand_base(mode, 'ds')
    cs_off = DATA + 0x20 - mem_operand_base(mode, 'cs')
    if 0 <= cs_off < (0x10000 if bits == 16 else 1 << 32):
        out.append(Case(mode, 'Prefixes', 'mov r32, cs:m32', [f'mov eax, [cs:{h(cs_off)}]'], rng))
    out.append(Case(mode, 'Prefixes', 'redundant DS override', [f'mov ebx, [ds:{h(off)}]'], rng))
    if bits == 16:
        out.append(Case(mode, 'Prefixes', '66 67 together', [f'a32 mov eax, [dword {h(off)}]', 'a32 o32 lea edx, [eax*2+ebx]'], rng,
                        regs={'ebx': rng.getrandbits(16)}))


def gen_p16(rng, out):
    """P16, the stretch under the stale real-mode CS: what the stubs run there (kvm-unit-tests' lgdtl, far JMP into
    32-bit code in its three forms, a data segment load, simple moves and logic, MOV CR0 back to real mode)."""
    mode = 'P16'
    gen_far(mode, rng, out)
    off = DATA + 0x10 - 0x20000
    for opsize, pre in ((32, 'o32 '), (16, '')):
        out.append(Case(mode, 'Segments and tables', f'lgdt m (o{opsize})', [f'{pre}lgdt [{h(off)}]'], rng,
                        mem={DATA + 0x10: GDT_LIMIT.to_bytes(2, 'little') + ((0x55000000 if opsize == 16 else 0) | GDT_ALT).to_bytes(4, 'little')}))
        out.append(Case(mode, 'Segments and tables', f'lidt m (o{opsize})', [f'{pre}lidt [{h(off)}]'], rng,
                        mem={DATA + 0x10: (0x3ff).to_bytes(2, 'little') + (0x00012000).to_bytes(4, 'little')}))
    for sreg, sel in (('ds', 0x10), ('es', 0x40), ('ss', 0x10), ('ds', 0x38)):
        d = loaded(sel)
        lin = d['base'] + 0x100 if d['base'] else 0x20100
        o = lin - d['base']
        body = [f'mov {sreg}, ax', f'mov [{sreg}:{"dword " if o > 0xffff else ""}{h(o)}], ecx'] if sreg != 'ss' else ['mov ss, ax', 'mov esp, 0x61000', 'push ecx']
        win = [(lin, 8)] if sreg != 'ss' else []
        out.append(Case(mode, 'Segments and tables', f'mov {sreg}, r16', body, rng, regs={'eax': sel}, windows=win + [(GDT + 0x38, 8)]))
    out.append(Case(mode, 'Data movement', 'mov r32, imm32', ['mov eax, 0x12345678', 'mov bx, 0x10'], rng))
    out.append(Case(mode, 'Data movement', 'mov r/m16, r16', [f'mov [{h(DATA + 2 - 0x20000)}], cx'], rng))
    out.append(Case(mode, 'Arithmetic and logic', 'or r/m32, imm8', ['or eax, 1', 'and ebx, 0xfffffff0'], rng, undefined=AF))
    out.append(Case(mode, 'Control registers, MSRs', 'mov r32, cr0', ['mov ebx, cr0'], rng, undefined=ARITH))
    out.append(Case(mode, 'Control registers, MSRs', 'mov r32, cr4', ['mov ebx, cr4'], rng, undefined=ARITH))
    out.append(Case(mode, 'Other', 'cli', ['cli'], rng, eflags=IF))
    out.append(Case(mode, 'Other', 'nop', ['nop'], rng))
    gen_control_p16(rng, out)


def gen_control_p16(rng, out):
    out.append(Case('P16', 'Control registers, MSRs', 'mov cr0, r32 (clear PE: P16 -> R)', ['mov cr0, eax'], rng,
                    regs={'eax': CR0_ET}, undefined=ARITH, end='R'))
    out.append(Case('P16', 'Control registers, MSRs', 'rdmsr (EFER)', ['rdmsr'], rng, regs={'ecx': 0xC0000080}))
    out.append(Case('P16', 'Control registers, MSRs', 'cpuid (leaf 0x1)', ['cpuid'], rng, regs={'eax': 1, 'ecx': 0},
                    by={r: 'profile' for r in ('eax', 'ebx', 'ecx', 'edx')}))


def gen_selftest(rng, out):
    """Empty cases: no instruction at all, so each reference must report exactly the starting state (with every flag
    pattern a case can start with): the rig's own check of its set-up and capture."""
    for mode in MODES:
        for flags in (0, ARITH | DF, IF | 0x3000 | 0x4000 | 0x40000 | 0x200000 | ARITH):
            out.append(Case(mode, 'Rig', 'empty case', [], rng, eflags=flags))


def generate(seed=SEED):
    rng = random.Random(seed)
    cases = []
    gen_selftest(rng, cases)
    for mode in ('R', 'P32', 'C32'):
        for g in (gen_mov, gen_movzx_lea, gen_alu, gen_test_incdec_shift, gen_bits, gen_flow, gen_far, gen_stack,
                  gen_segments, gen_control, gen_other):
            g(mode, rng, cases)
    gen_p16(rng, cases)
    return cases


# --- the corpus: cases resolved into the format ---

def corpus(seed=SEED) -> dict:
    """The corpus in the format (docs/restructure/interp-cases.md), with each case's reference-only bytes (its capture
    blobs and P16 prologue, which only the rig places) under `rig`."""
    import interp_rig
    cases = generate(seed)
    for bits in (16, 32):
        sel = [c for c in cases if c.bits == bits]
        for c, code in zip(sel, assemble([c.asm for c in sel], bits)): c.code = code
    out, names = [], {}
    for c in cases:
        slug = f'{c.mode}-{c.form}'
        names[slug] = names.get(slug, -1) + 1
        name = f'{slug}-{names[slug]:02d}'.replace(' ', '_')
        out.append(resolve(c, name, interp_rig))
    blocks = [{'name': n, 'addr': h(a), 'data': d.hex()} for n, a, d in common_blocks()]
    return {'format': FORMAT, 'generator': 'tests/interp/interp_cases.py', 'seed': seed, 'common': blocks, 'cases': out}


def resolve(c, name, rig) -> dict:
    cs = c.segs['cs']
    ip = CODE - cs['base']
    fall = (CODE + len(c.code), cs['sel'], ip + len(c.code), c.end)
    if c.end != c.mode:     # a mode change at the end: the fall-through's CS is the same register, now read in the new mode
        fall = (CODE + len(c.code), cs['sel'], ip + len(c.code), c.end)
    stops = [fall, *c.extra_stops]
    mem = dict(c.mem)
    entry_eip, entry_esp = ip, c.gpr[GPRS.index('esp')]
    rig_bytes = []
    if c.mode == 'P16':     # EFLAGS just below SP (the prologue's POPFD takes them), EAX in its slot
        esp = c.gpr[GPRS.index('esp')]
        lin = c.segs['ss']['base'] + esp - 4
        mem.update({lin + i: b for i, b in enumerate(c.eflags.to_bytes(4, 'little'))})
        slot = ip - P16_PROLOGUE
        pro = (c.gpr[0].to_bytes(4, 'little') + bytes([0x0f, 0x22, 0xc0, 0x66, 0x9d, 0x2e, 0x66, 0xa1]) + slot.to_bytes(2, 'little'))
        assert len(pro) == P16_PROLOGUE
        rig_bytes.append({'addr': h(CODE - P16_PROLOGUE), 'data': pro.hex(), 'what': 'P16 prologue: EAX, MOV CR0,EAX; POPFD; MOV EAX,[CS:slot]'})
        entry_eip, entry_esp = ip - P16_PROLOGUE + 4, esp - 4
    for i, (lin, sel, eip, end) in enumerate(stops):
        rig_bytes.append({'addr': h(lin), 'data': rig.capture(end, i).hex(), 'what': f'capture, stop {i} ({end})'})
    # memory: runs of the byte map
    runs, addrs = [], sorted(mem)
    for a in addrs:
        if runs and runs[-1][0] + len(runs[-1][1]) == a: runs[-1][1].append(mem[a])
        else: runs.append([a, [mem[a]]])
    code_end = CODE + len(c.code)
    for lin, _, _, _ in stops:
        for a, d in runs:
            assert not (a < lin + 0x100 and lin < a + len(d)), f'{name}: memory at {a:#x} overlaps stop {lin:#x}'
    und = c.undefined
    by = {f: 'both' for f in (*GPRS, 'eflags', *SREGS, 'cr0', 'cr3', 'cr4', 'efer', 'gdtr', 'idtr', 'stop')}
    by.update({f'window{i}': 'both' for i in range(len(c.windows))})
    by.update(c.by)
    ctl = {'cr0': h(c.ctl['cr0']), 'cr3': h(c.ctl['cr3']), 'cr4': h(c.ctl['cr4']), 'efer': h(c.ctl['efer'])}
    case = {
        'name': name, 'group': c.group, 'form': c.form, 'mode': c.mode, 'note': c.note,
        'state': {
            'gpr': {r: h(v) for r, v in zip(GPRS, c.gpr)},
            'eip': h(ip), 'eflags': h(c.eflags), 'cpl': 0,
            'seg': {s: {k: h(v) for k, v in c.segs[s].items()} for s in SREGS},
            **ctl,
            'gdtr': {'base': h(c.gdtr[0]), 'limit': h(c.gdtr[1])}, 'idtr': {'base': h(c.idtr[0]), 'limit': h(c.idtr[1])},
        },
        'code': {'addr': h(CODE), 'data': c.code.hex(), 'asm': c.asm},
        'memory': [{'addr': h(a), 'data': bytes(d).hex()} for a, d in runs],
        'stops': [{'id': i, 'linear': h(lin), 'cs': h(sel), 'eip': h(eip), 'mode': end} for i, (lin, sel, eip, end) in enumerate(stops)],
        'expect_stop': 1 if c.extra_stops else 0,
        'compare': {
            'gpr': {r: h(0xffffffff) for r in GPRS},
            'eflags': h(EFLAGS_MASK & ~und), 'cr0': h(CR0_MASK), 'cr3': h(0xffffffff), 'cr4': h(CR4_MASK), 'efer': h(EFER_MASK),
            'windows': [{'addr': h(a), 'len': n} for a, n in c.windows],
            'by': {k: v for k, v in sorted(by.items()) if v != 'both'},
        },
        'rig': {'entry_eip': h(entry_eip), 'entry_esp': h(entry_esp), 'bytes': rig_bytes},
    }
    case['hash'] = case_hash(case)
    return case


def case_hash(case) -> str:
    """What decides a case's answers: everything but its name, notes and this hash (the answers' lock keys use it)."""
    keep = {k: v for k, v in case.items() if k not in ('name', 'note', 'hash', 'group', 'form')}
    return hashlib.sha256(json.dumps(keep, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('-o', '--output', required=True)
    ap.add_argument('--seed', type=int, default=SEED)
    a = ap.parse_args(argv)
    doc = corpus(a.seed)
    pathlib.Path(a.output).write_text(json.dumps(doc, indent=1, sort_keys=True) + '\n')
    print(f'{len(doc["cases"])} cases -> {a.output}')


if __name__ == '__main__':
    testenv.require_framework(__file__)
    main()

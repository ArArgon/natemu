"""Independent checker for the ACPI tables the natemu loader writes (boot-features plan L2; replaces `iasl -d`).

    check(dump_bytes, n) -> dict(violations=[...], not_fixed=[...], listing=str, info=dict)

Inputs are the driver's `--dump` records ([u64 gpa][u64 length][bytes], later records override earlier ones; guest RAM is
zero elsewhere) and the vCPU count N. Expectations are derived from: ACPI 6.6 ch. 5 snapshot (layouts; the plan targets
ACPI 6.5 and the version-0 16-byte multiprocessor wakeup structure of ACPI 6.4/6.5), ACPI ch. 19/20 AML grammar and
ch. 6.4 resource descriptors (from knowledge, not in the snapshot), CLM-03-03-22/23/24/69, the boot-features plan
(§1a AP start / legacy island / idle states, §1e ACPI tables, L1/X3 notes), Linux v6.12 consumers
(arch/x86/kernel/acpi/boot.c, madt_wakeup.c, drivers/acpi/processor_idle.c) and tools/models/cpu_profile, plus the
main session's decisions: the SCI as QEMU describes it (IRQ9 -> GSI9 override, flags 0x000d), an RTC device (PNP0B00,
0x70/2, IRQ8), FADT flags judged against QEMU's PC FADT, GPE0 omitted.

Violations are strings `RULE: message` (with guest physical addresses and table offsets). Values the records do not fix
go to `not_fixed` as dict(item, value, meaning, judgement) where judgement starts with 'FLAG' when it looks
inconsistent with the machine; they never fail the check."""
import struct

try:
    from . import cpu_profile
except ImportError:  # run as a script from tools/models
    import cpu_profile

ZP = 0x10000
NVS = (0x600000, 0x800000)          # CLM-03-03-69: RSDP, table graph, FACS and the 4 KiB version-0 mailbox
IOAPIC_BASE, LAPIC_BASE = 0xFEC00000, 0xFEE00000
PMBASE, PM_LEN, GPE0, GPE0_LEN, RESET_PORT, RESET_VALUE = 0xB000, 0x40, 0xAFE0, 4, 0xB040, 0x06
SCI = 9
SCI_FLAGS = 0x000D          # main-session decision: IRQ9 -> GSI9, level, active high, as QEMU's PC MADT
QEMU_INT_MODEL = 1


def qemu_flags(n):
    """QEMU v10 hw/i386/acpi-build.c PC FADT flags (from memory of that source; no snapshot in the repo):
    WBINVD, PROC_C1, SLP_BUTTON, RTC_S4, USE_PLATFORM_CLOCK, FORCE_APIC_CLUSTER_MODEL above 8 CPUs."""
    return (1 << 0) | (1 << 2) | (1 << 5) | (1 << 7) | (1 << 15) | ((n > 8) << 18)


class Image:
    """Guest memory from dump records; keeps the written extents."""

    def __init__(self, dump):
        self.recs, o = [], 0
        while o < len(dump):
            a, n = struct.unpack_from('<QQ', dump, o)
            self.recs.append((a, bytes(dump[o + 16:o + 16 + n])))
            o += 16 + n
        if o != len(dump):
            raise ValueError('dump: truncated record')

    def read(self, a, n):
        out = bytearray(n)
        for g, b in self.recs:
            lo, hi = max(a, g), min(a + n, g + len(b))
            if lo < hi:
                out[lo - a:hi - a] = b[lo - g:hi - g]
        return bytes(out)

    def write(self, a, b):  # for negative controls
        self.recs.append((a, bytes(b)))

    def written(self, a, n):
        """True if every byte of [a, a+n) was written by some record."""
        iv = sorted((max(g, a), min(g + len(b), a + n)) for g, b in self.recs if g < a + n and g + len(b) > a)
        cur = a
        for lo, hi in iv:
            if lo > cur:
                return False
            cur = max(cur, hi)
        return cur >= a + n

    def extent_at(self, a):
        """Length of the write record starting exactly at a (the latest), or None."""
        hits = [len(b) for g, b in self.recs if g == a]
        return hits[-1] if hits else None


u8 = lambda b, o: b[o]                                    # noqa: E731
u16 = lambda b, o: struct.unpack_from('<H', b, o)[0]      # noqa: E731
u32 = lambda b, o: struct.unpack_from('<I', b, o)[0]      # noqa: E731
u64 = lambda b, o: struct.unpack_from('<Q', b, o)[0]      # noqa: E731


def eisaid(v):
    b = struct.pack('<I', v)
    c = ((b[0] >> 2) & 31, ((b[0] & 3) << 3) | (b[1] >> 5), b[1] & 31)
    return ''.join(chr(0x40 + x) for x in c) + f'{b[2]:02X}{b[3]:02X}'


# ---------------------------------------------------------------------------------------------------------------------
# AML (ACPI 6.5 ch. 20): a full decoder for the subset a static DSDT may use; anything else is an error.

class AmlError(Exception):
    pass


NAMED_SIMPLE = {0x08: 'Name', 0x10: 'Scope', 0x14: 'Method', 0x06: 'Alias'}
# Expression / statement opcodes: name and argument kinds (T TermArg, S SuperName, R Target, b byte, w word).
EXPR = {0x70: ('Store', 'TS'), 0x71: ('RefOf', 'S'), 0x72: ('Add', 'TTR'), 0x73: ('Concatenate', 'TTR'),
        0x74: ('Subtract', 'TTR'), 0x75: ('Increment', 'S'), 0x76: ('Decrement', 'S'), 0x77: ('Multiply', 'TTR'),
        0x78: ('Divide', 'TTRR'), 0x79: ('ShiftLeft', 'TTR'), 0x7A: ('ShiftRight', 'TTR'), 0x7B: ('And', 'TTR'),
        0x7C: ('NAnd', 'TTR'), 0x7D: ('Or', 'TTR'), 0x7E: ('NOr', 'TTR'), 0x7F: ('XOr', 'TTR'), 0x80: ('Not', 'TR'),
        0x83: ('DerefOf', 'T'), 0x86: ('Notify', 'ST'), 0x87: ('SizeOf', 'S'), 0x88: ('Index', 'TTR'),
        0x8E: ('ObjectType', 'S'), 0x90: ('LAnd', 'TT'), 0x91: ('LOr', 'TT'), 0x92: ('LNot', 'T'),
        0x93: ('LEqual', 'TT'), 0x94: ('LGreater', 'TT'), 0x95: ('LLess', 'TT'), 0xA3: ('Noop', ''),
        0xA4: ('Return', 'T'), 0xA5: ('Break', ''), 0xCC: ('BreakPoint', '')}
EXT_EXPR = {0x21: ('Stall', 'T'), 0x22: ('Sleep', 'T'), 0x23: ('Acquire', 'Sw'), 0x27: ('Release', 'S'),
            0x30: ('Revision', '')}
REGION_SPACES = {0: 'SystemMemory', 1: 'SystemIO', 2: 'PCI_Config', 3: 'EmbeddedControl', 4: 'SMBus', 5: 'SystemCMOS',
                 6: 'PciBarTarget', 7: 'IPMI', 8: 'GeneralPurposeIo', 9: 'GenericSerialBus', 0xA: 'PCC'}


class Aml:
    """Parses a whole TermList; builds `objects` {absolute path: (kind, value, offset)} and an ASL-like listing."""

    def __init__(self, data, gpa):
        self.d, self.gpa = data, gpa          # gpa of data[0] (for messages)
        self.lines, self.objects, self.methods = [], {}, {}

    def err(self, i, msg):
        raise AmlError(f'{msg} at AML byte {i:#x} (gpa {self.gpa + i:#x})')

    def byte(self, i, end):
        if i >= end:
            self.err(i, 'truncated')
        return self.d[i]

    def pkglen(self, i, end):
        lead = self.byte(i, end)
        n = lead >> 6
        if n == 0:
            v = lead & 0x3F
        else:
            if lead & 0x30:
                self.err(i, f'PkgLength lead byte {lead:#04x}: bits 4-5 must be zero in a multi-byte encoding')
            v = lead & 0x0F
            for k in range(n):
                v |= self.byte(i + 1 + k, end) << (4 + 8 * k)
        stop = i + v
        if v < n + 1 or stop > end:
            self.err(i, f'PkgLength {v:#x} ends at {stop:#x}, outside its enclosing object (end {end:#x})')
        return stop, i + 1 + n

    def nameseg(self, i, end):
        s = self.d[i:i + 4]
        if i + 4 > end or not (chr(s[0]) in '_ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                               and all(chr(c) in '_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789' for c in s[1:])):
            self.err(i, f'bad NameSeg {s!r}')
        return s.decode(), i + 4

    def namestring(self, i, end):
        pre = ''
        if self.byte(i, end) == 0x5C:
            pre, i = '\\', i + 1
        else:
            while self.byte(i, end) == 0x5E:
                pre, i = pre + '^', i + 1
        c = self.byte(i, end)
        if c == 0x00:
            return pre, i + 1
        if c == 0x2E:
            n, i = 2, i + 1
        elif c == 0x2F:
            n, i = self.byte(i + 1, end), i + 2
            if n == 0:
                self.err(i - 1, 'MultiNamePrefix with zero segments')
        else:
            n = 1
        segs = []
        for _ in range(n):
            s, i = self.nameseg(i, end)
            segs.append(s)
        return pre + '.'.join(segs), i

    @staticmethod
    def resolve(scope, name):
        if name.startswith('\\'):
            return name if name != '\\' else '\\'
        parts = scope.strip('\\').split('.') if scope != '\\' else []
        while name.startswith('^'):
            if not parts:
                raise AmlError(f'{name}: parent prefix above the root')
            parts.pop()
            name = name[1:]
        return '\\' + '.'.join(parts + ([name] if name else []))

    def out(self, i, depth, text):
        self.lines.append(f'{self.gpa + i:08x}  {"    " * depth}{text}')

    def define(self, path, kind, value, i):
        if path in self.objects:
            self.err(i, f'{path} defined twice')
        self.objects[path] = (kind, value, i)

    # -- data --
    def integer(self, i, end):
        """(value, next, text) for a ComputationalData integer, or None."""
        c = self.byte(i, end)
        if c in (0x00, 0x01):
            return c, i + 1, 'Zero' if c == 0 else 'One'
        if c == 0xFF:
            return (1 << 64) - 1, i + 1, 'Ones'
        for p, n in ((0x0A, 1), (0x0B, 2), (0x0C, 4), (0x0E, 8)):
            if c == p:
                if i + 1 + n > end:
                    self.err(i, 'truncated integer')
                v = int.from_bytes(self.d[i + 1:i + 1 + n], 'little')
                return v, i + 1 + n, f'{v:#x}'
        if c == 0x5B and self.byte(i + 1, end) == 0x30:
            return 'Revision', i + 2, 'Revision'
        return None

    def data(self, i, end, depth):
        """DataRefObject: integer, string, buffer, package. Returns (value, next, text)."""
        r = self.integer(i, end)
        if r:
            return r
        c = self.d[i]
        if c == 0x0D:
            j = i + 1
            while self.byte(j, end) != 0:
                if self.d[j] > 0x7F:
                    self.err(j, 'string byte above 0x7f')
                j += 1
            s = self.d[i + 1:j].decode()
            return s, j + 1, repr(s).replace("'", '"')
        if c == 0x11:
            stop, j = self.pkglen(i + 1, end)
            size, j, _ = self.termarg(j, stop, depth)
            if not isinstance(size, int):
                self.err(i, 'Buffer size is not a constant integer')
            init = self.d[j:stop]
            if len(init) > size:
                self.err(i, f'Buffer initializer {len(init)} bytes exceeds BufferSize {size}')
            return ('buffer', size, bytes(init)), stop, f'Buffer({size:#x}) {{{init.hex(" ")}}}'
        if c in (0x12, 0x13):
            stop, j = self.pkglen(i + 1, end)
            if c == 0x12:
                count, j = self.byte(j, stop), j + 1
            else:
                count, j, _ = self.termarg(j, stop, depth)
            elems, texts = [], []
            while j < stop:
                if self.d[j] in (0x5C, 0x5E, 0x2E, 0x2F) or chr(self.d[j]) in '_ABCDEFGHIJKLMNOPQRSTUVWXYZ':
                    v, j = self.namestring(j, stop)
                    v, t = ('name', v), v
                else:
                    v, j, t = self.data(j, stop, depth)
                elems.append(v)
                texts.append(t)
            if isinstance(count, int) and len(elems) > count:
                self.err(i, f'Package has {len(elems)} elements, NumElements {count}')
            return ('package', count, elems), stop, f'Package({count}) {{{", ".join(texts)}}}'
        self.err(i, f'opcode {c:#04x} is not a data object')

    # -- terms --
    def supername(self, i, end, target=False):
        c = self.byte(i, end)
        if 0x60 <= c <= 0x67:
            return f'Local{c - 0x60}', i + 1
        if 0x68 <= c <= 0x6E:
            return f'Arg{c - 0x68}', i + 1
        if c == 0x00 and target:
            return '', i + 1
        if c == 0x5B and self.byte(i + 1, end) == 0x31:
            return 'Debug', i + 2
        if c in (0x83, 0x88, 0x71):
            _, j, t = self.termarg(i, end, 0)
            return t, j
        return self.namestring(i, end)

    def termarg(self, i, end, depth):
        """(constant value or None, next, text)."""
        c = self.byte(i, end)
        r = self.integer(i, end)
        if r:
            return r
        if c in (0x0D, 0x11, 0x12, 0x13):
            v, j, t = self.data(i, end, depth)
            return v, j, t
        if 0x60 <= c <= 0x6E:
            t, j = self.supername(i, end)
            return None, j, t
        if c in EXPR or (c == 0x5B and self.byte(i + 1, end) in EXT_EXPR):
            return None, *self.expr(i, end)
        if c in (0x5C, 0x5E, 0x2E, 0x2F) or chr(c) in '_ABCDEFGHIJKLMNOPQRSTUVWXYZ':
            name, j = self.namestring(i, end)
            args = []
            for _ in range(self.methods.get(name.split('.')[-1].lstrip('\\^'), 0)):
                _, j, t = self.termarg(j, end, depth)
                args.append(t)
            return None, j, f'{name}({", ".join(args)})' if args else name
        self.err(i, f'unknown opcode {c:#04x} in a TermArg')

    def expr(self, i, end):
        c = self.d[i]
        name, kinds = (EXT_EXPR[self.d[i + 1]] if c == 0x5B else EXPR[c])
        j = i + (2 if c == 0x5B else 1)
        args = []
        for k in kinds:
            if k == 'T':
                _, j, t = self.termarg(j, end, 0)
            elif k in 'SR':
                t, j = self.supername(j, end, target=k == 'R')
            elif k == 'w':
                t, j = f'{u16(self.d, j) if j + 2 <= end else self.err(j, "truncated"):#x}', j + 2
            args.append(t)
        return j, f'{name}({", ".join(a for a in args if a != "")})'

    def termlist(self, i, end, scope, depth):
        while i < end:
            i = self.term(i, end, scope, depth)
        if i != end:
            self.err(i, 'term overruns its enclosing object')

    def term(self, i, end, scope, depth):
        c = self.d[i]
        if c == 0x08:
            name, j = self.namestring(i + 1, end)
            v, j, t = self.data(j, end, depth)
            self.define(self.resolve(scope, name), 'Name', v, i)
            if name.endswith('_HID') and isinstance(v, int):
                t = f'EisaId ("{eisaid(v)}")'
            self.out(i, depth, f'Name ({name}, {t})')
            if isinstance(v, tuple) and v[0] == 'buffer' and name.endswith(('_CRS', '_PRS')):
                its, probs = decode_resources(v[2])
                for x in its + probs:
                    self.out(i, depth + 1, f'// {x}')
            return j
        if c == 0x06:
            a, j = self.namestring(i + 1, end)
            b, j = self.namestring(j, end)
            self.define(self.resolve(scope, b), 'Alias', self.resolve(scope, a), i)
            self.out(i, depth, f'Alias ({a}, {b})')
            return j
        if c == 0x10:
            stop, j = self.pkglen(i + 1, end)
            name, j = self.namestring(j, stop)
            self.out(i, depth, f'Scope ({name})')
            self.out(i, depth, '{')
            self.termlist(j, stop, self.resolve(scope, name), depth + 1)
            self.out(stop - 1, depth, '}')
            return stop
        if c == 0x14:
            stop, j = self.pkglen(i + 1, end)
            name, j = self.namestring(j, stop)
            flags = self.byte(j, stop)
            path = self.resolve(scope, name)
            self.define(path, 'Method', flags, i)
            self.methods[name.split('.')[-1].lstrip('\\^')] = flags & 7
            self.out(i, depth, f'Method ({name}, {flags & 7}, {"Serialized" if flags & 8 else "NotSerialized"})')
            self.out(i, depth, '{')
            self.termlist(j + 1, stop, path, depth + 1)
            self.out(stop - 1, depth, '}')
            return stop
        if c in (0xA0, 0xA2):
            stop, j = self.pkglen(i + 1, end)
            _, j, t = self.termarg(j, stop, depth)
            self.out(i, depth, f'{"If" if c == 0xA0 else "While"} ({t})')
            self.out(i, depth, '{')
            self.termlist(j, stop, scope, depth + 1)
            self.out(stop - 1, depth, '}')
            return stop
        if c == 0xA1:
            stop, j = self.pkglen(i + 1, end)
            self.out(i, depth, 'Else')
            self.out(i, depth, '{')
            self.termlist(j, stop, scope, depth + 1)
            self.out(stop - 1, depth, '}')
            return stop
        if c == 0x5B:
            e = self.byte(i + 1, end)
            if e in (0x82, 0x83, 0x84, 0x85):
                stop, j = self.pkglen(i + 2, end)
                name, j = self.namestring(j, stop)
                path = self.resolve(scope, name)
                kind = {0x82: 'Device', 0x83: 'Processor', 0x84: 'PowerResource', 0x85: 'ThermalZone'}[e]
                extra, val = '', None
                if e == 0x83:
                    if j + 6 > stop:
                        self.err(j, 'truncated Processor')
                    val = (self.d[j], u32(self.d, j + 1), self.d[j + 5])
                    extra, j = f', {val[0]:#x}, {val[1]:#010x}, {val[2]:#x}', j + 6
                elif e == 0x84:
                    if j + 3 > stop:
                        self.err(j, 'truncated PowerResource')
                    extra, j = f', {self.d[j]}, {u16(self.d, j + 1)}', j + 3
                self.define(path, kind, val, i)
                self.out(i, depth, f'{kind} ({name}{extra})')
                self.out(i, depth, '{')
                self.termlist(j, stop, path, depth + 1)
                self.out(stop - 1, depth, '}')
                return stop
            if e == 0x80:
                name, j = self.namestring(i + 2, end)
                space = self.byte(j, end)
                if space not in REGION_SPACES and space < 0x80:
                    self.err(j, f'OperationRegion space {space:#x} reserved')
                off, j, to = self.termarg(j + 1, end, depth)
                ln, j, tl = self.termarg(j, end, depth)
                self.define(self.resolve(scope, name), 'OperationRegion', (space, off, ln), i)
                self.out(i, depth, f'OperationRegion ({name}, {REGION_SPACES.get(space, hex(space))}, {to}, {tl})')
                return j
            if e == 0x81:
                stop, j = self.pkglen(i + 2, end)
                region, j = self.namestring(j, stop)
                flags = self.byte(j, stop)
                if flags & 0x80 or (flags & 15) > 5 or (flags >> 5 & 3) == 3:
                    self.err(j, f'Field flags {flags:#x} reserved bits/values')
                self.out(i, depth, f'Field ({region}, {flags:#04x})')
                self.out(i, depth, '{')
                j, bit = j + 1, 0
                while j < stop:
                    k = self.d[j]
                    if k == 0x00:
                        n, j2 = self.pkglen_value(j + 1, stop)
                        self.out(j, depth + 1, f'Offset/reserved ({n} bits)')
                    elif k == 0x01:
                        if j + 3 > stop:
                            self.err(j, 'truncated AccessField')
                        self.out(j, depth + 1, f'AccessAs ({self.d[j + 1]:#x}, {self.d[j + 2]:#x})')
                        n, j2 = 0, j + 3
                    else:
                        seg, j1 = self.nameseg(j, stop)
                        n, j2 = self.pkglen_value(j1, stop)
                        self.define(self.resolve(scope, seg), 'FieldUnit', (region, bit, n), j)
                        self.out(j, depth + 1, f'{seg}, {n}')
                    bit, j = bit + n, j2
                self.out(stop - 1, depth, '}')
                return stop
            if e == 0x01:
                name, j = self.namestring(i + 2, end)
                sync = self.byte(j, end)
                if sync > 15:
                    self.err(j, f'Mutex SyncFlags {sync:#x}')
                self.define(self.resolve(scope, name), 'Mutex', sync, i)
                self.out(i, depth, f'Mutex ({name}, {sync})')
                return j + 1
            if e == 0x02:
                name, j = self.namestring(i + 2, end)
                self.define(self.resolve(scope, name), 'Event', None, i)
                self.out(i, depth, f'Event ({name})')
                return j
            if e in EXT_EXPR and e != 0x30:
                j, t = self.expr(i, end)
                self.out(i, depth, t)
                return j
            self.err(i, f'unknown extended opcode 0x5b {e:#04x}')
        if c in EXPR:
            j, t = self.expr(i, end)
            self.out(i, depth, t)
            return j
        if (c in (0x5C, 0x5E, 0x2E, 0x2F) or chr(c) in '_ABCDEFGHIJKLMNOPQRSTUVWXYZ') and scope != '\\' and \
                self.objects.get(scope, ('',))[0] == 'Method':
            _, j, t = self.termarg(i, end, depth)          # a method invocation statement
            self.out(i, depth, t)
            return j
        self.err(i, f'unknown opcode {c:#04x} in a TermList')

    def pkglen_value(self, i, end):
        """A PkgLength used as a plain value (field widths): (value, next)."""
        lead = self.byte(i, end)
        n = lead >> 6
        if n and lead & 0x30:
            self.err(i, f'PkgLength lead byte {lead:#04x}: bits 4-5 must be zero in a multi-byte encoding')
        v = lead & (0x3F if n == 0 else 0x0F)
        for k in range(n):
            v |= self.byte(i + 1 + k, end) << (4 + 8 * k)
        return v, i + 1 + n


def decode_resources(buf):
    """ACPI 6.5 §6.4 resource template -> (items, problems). items: dicts with 'type' and fields."""
    items, probs, i, ended = [], [], 0, False
    small_len = {0x4: (2, 3), 0x5: (2,), 0x6: (0, 1), 0x7: (0,), 0x8: (7,), 0x9: (3,), 0xA: (5,), 0xF: (1,)}
    while i < len(buf):
        if ended:
            probs.append(f'bytes after the end tag at template offset {i:#x}')
            break
        t = buf[i]
        if not t & 0x80:
            typ, ln = (t >> 3) & 0xF, t & 7
            if typ == 0xE:
                ok = 1 <= ln <= 7
            else:
                ok = ln in small_len.get(typ, ())
            if not ok:
                probs.append(f'small item type {typ:#x} length {ln} invalid at template offset {i:#x}')
                break
            body = buf[i + 1:i + 1 + ln]
            if len(body) < ln:
                probs.append(f'truncated item at template offset {i:#x}')
                break
            if typ == 0x4:
                mask = u16(body, 0)
                info = body[2] if ln == 3 else 0x01          # default: edge, active high, exclusive
                if info & 0xC6:
                    probs.append(f'IRQ info byte {info:#x} reserved bits')
                items.append(dict(type='IRQ', irqs=[k for k in range(16) if mask >> k & 1], edge=bool(info & 1),
                                  low=bool(info & 8), shared=bool(info & 0x10), off=i))
            elif typ == 0x8:
                info, lo, hi, al, n = body[0], u16(body, 1), u16(body, 3), body[5], body[6]
                if info & 0xFE:
                    probs.append(f'IO info byte {info:#x} reserved bits')
                if lo > hi:
                    probs.append(f'IO min {lo:#x} > max {hi:#x}')
                items.append(dict(type='IO', decode16=bool(info & 1), min=lo, max=hi, align=al, len=n, off=i))
            elif typ == 0x9:
                items.append(dict(type='FixedIO', base=u16(body, 0) & 0x3FF, len=body[2], off=i))
            elif typ == 0xF:
                total = sum(buf[:i + 2]) & 0xFF
                if body[0] != 0 and total != 0:
                    probs.append(f'end tag checksum {body[0]:#04x}: template sums to {total:#04x}, not zero')
                items.append(dict(type='End', checksum=body[0], off=i))
                ended = True
            else:
                items.append(dict(type=f'small{typ:#x}', raw=body.hex(), off=i))
            i += 1 + ln
        else:
            if i + 3 > len(buf):
                probs.append(f'truncated large item at template offset {i:#x}')
                break
            typ, ln = t & 0x7F, u16(buf, i + 1)
            body = buf[i + 3:i + 3 + ln]
            minlen = {0x1: 9, 0x2: 12, 0x4: 1, 0x5: 17, 0x6: 9, 0x7: 23, 0x8: 13, 0x9: 6, 0xA: 43, 0xB: 53}
            if typ not in minlen or ln < minlen[typ] or len(body) < ln:
                probs.append(f'large item type {typ:#x} length {ln} invalid at template offset {i:#x}')
                break
            if typ == 0x6:
                items.append(dict(type='Memory32Fixed', rw=bool(body[0] & 1), base=u32(body, 1), len=u32(body, 5), off=i))
            elif typ == 0x9:
                cnt = body[1]
                items.append(dict(type='Interrupt', consumer=bool(body[0] & 1), level=not body[0] & 2,
                                  low=bool(body[0] & 4), shared=bool(body[0] & 8),
                                  gsis=[u32(body, 2 + 4 * k) for k in range(cnt)], off=i))
            else:
                items.append(dict(type=f'large{typ:#x}', raw=body.hex(), off=i))
            i += 3 + ln
    if not ended:
        probs.append('no end tag')
    return items, probs


# ---------------------------------------------------------------------------------------------------------------------

FADT_FLAGS = ['WBINVD', 'WBINVD_FLUSH', 'PROC_C1', 'P_LVL2_UP', 'PWR_BUTTON', 'SLP_BUTTON', 'FIX_RTC', 'RTC_S4',
              'TMR_VAL_EXT', 'DCK_CAP', 'RESET_REG_SUP', 'SEALED_CASE', 'HEADLESS', 'CPU_SW_SLP', 'PCI_EXP_WAK',
              'USE_PLATFORM_CLOCK', 'S4_RTC_STS_VALID', 'REMOTE_POWER_ON_CAPABLE', 'FORCE_APIC_CLUSTER_MODEL',
              'FORCE_APIC_PHYSICAL_DESTINATION_MODE', 'HW_REDUCED_ACPI', 'LOW_POWER_S0_IDLE_CAPABLE']
IAPC = ['LEGACY_DEVICES', '8042', 'VGA_NOT_PRESENT', 'MSI_NOT_SUPPORTED', 'PCIE_ASPM_CONTROLS', 'CMOS_RTC_NOT_PRESENT']
MADT_LEN = {0: 8, 1: 12, 2: 10, 3: 8, 4: 6, 5: 12, 9: 16, 0xA: 12, 0x10: 16}


def gas(b, o):
    return dict(space=b[o], width=b[o + 1], offset=b[o + 2], access=b[o + 3], addr=u64(b, o + 4))


def fmt_gas(g):
    return f"space {g['space']} width {g['width']} offset {g['offset']} access {g['access']} addr {g['addr']:#x}"


def check(dump, n, profile=None, corrupt=None):
    img = Image(dump)
    if corrupt:
        corrupt(img)
    prof = profile or cpu_profile.load()
    V, NF, info = [], [], {}
    viol = lambda rule, msg: V.append(f'{rule}: {msg}')                                   # noqa: E731
    nf = lambda item, value, meaning, judgement='ok': NF.append(dict(item=item, value=value, meaning=meaning,  # noqa
                                                                       judgement=judgement))
    zp = img.read(ZP, 4096)
    e820 = [(u64(zp, 0x2D0 + 20 * k), u64(zp, 0x2D8 + 20 * k), u32(zp, 0x2E0 + 20 * k)) for k in range(zp[0x1E8])]

    def e820_of(a, ln):
        for s, size, t in e820:
            if s <= a and a + ln <= s + size:
                return (s, size, t)
        return None

    tables = {}          # name -> (gpa, length)

    def header(p, sig, name):
        """Reads and validates a description header at p; returns the table bytes or None."""
        if p == 0:
            viol('POINTER', f'{name}: null pointer')
            return None
        h = img.read(p, 36)
        if h[:4] != sig:
            viol('POINTER', f'{name} at {p:#x}: signature {h[:4]!r}, expected {sig!r}')
            return None
        ln = u32(h, 4)
        if ln < 36 or ln > NVS[1] - NVS[0]:
            viol('LENGTH', f'{name} at {p:#x}: header length {ln:#x}')
            return None
        if not img.written(p, ln):
            viol('POINTER', f'{name} at {p:#x}+{ln:#x}: not entirely written by the loader')
        rec = img.extent_at(p)
        if rec is not None and rec != ln:
            viol('LENGTH', f'{name} at {p:#x}: header length {ln:#x}, the write record there is {rec:#x} bytes')
        b = img.read(p, ln)
        if sum(b) & 0xFF:
            viol('CHECKSUM', f'{name} at {p:#x}: bytes sum to {sum(b) & 0xFF:#04x} (checksum byte {b[9]:#04x} at +9)')
        tables[name] = (p, ln)
        info[name] = dict(gpa=hex(p), length=ln, revision=b[8], oem=b[10:16].decode('latin1'),
                          oem_table=b[16:24].decode('latin1'), oem_rev=u32(b, 24),
                          creator=b[28:32].decode('latin1'), creator_rev=u32(b, 32))
        return b

    # ---- RSDP ------------------------------------------------------------------------------------------------------
    rp = u64(zp, 0x70)
    rs = img.read(rp, 36)
    info['RSDP'] = dict(gpa=hex(rp))
    if rs[:8] != b'RSD PTR ':
        viol('RSDP', f'acpi_rsdp_addr {rp:#x}: signature {rs[:8]!r}')
        return dict(violations=V, not_fixed=NF, listing='', info=info)
    tables['RSDP'] = (rp, 36)
    if sum(rs[:20]) & 0xFF:
        viol('CHECKSUM', f'RSDP at {rp:#x}: first 20 bytes sum to {sum(rs[:20]) & 0xFF:#04x}')
    if rs[15] != 2:
        viol('REVISION', f'RSDP revision {rs[15]} (ACPI 2.0+ form is 2)')
    if u32(rs, 20) != 36:
        viol('LENGTH', f'RSDP length {u32(rs, 20)} (36)')
    if sum(rs) & 0xFF:
        viol('CHECKSUM', f'RSDP at {rp:#x}: extended checksum, 36 bytes sum to {sum(rs) & 0xFF:#04x}')
    if any(rs[33:36]):
        viol('RSDP', f'reserved bytes 33..35 = {rs[33:36].hex()}')
    if not img.written(rp, 36):
        viol('POINTER', f'RSDP at {rp:#x}: not written by the loader')
    nf('RSDP OEMID', rs[9:15].decode('latin1'), 'OEM identification', 'ok')
    rsdt = u32(rs, 16)
    nf('RSDP RsdtAddress', hex(rsdt), 'no RSDT; OSPM uses the XSDT (revision 2)' if rsdt == 0 else 'RSDT present',
       'ok' if rsdt == 0 else 'FLAG: an RSDT is outside the claimed table set; check it matches the XSDT')
    nf('RSDP alignment', hex(rp & 15), '16-byte alignment matters only for the legacy BIOS-area scan',
       'ok' if rp % 16 == 0 else 'FLAG: not 16-byte aligned')

    # ---- XSDT ------------------------------------------------------------------------------------------------------
    xs = header(u64(rs, 24), b'XSDT', 'XSDT')
    sigs = {}
    if xs:
        if xs[8] != 1:
            viol('REVISION', f'XSDT revision {xs[8]} (1)')
        if (len(xs) - 36) % 8:
            viol('LENGTH', f'XSDT length {len(xs)}: entries are not whole u64s')
        for k in range(36, len(xs) - 7, 8):
            p = u64(xs, k)
            s = img.read(p, 4)
            if s in sigs.values():
                viol('XSDT', f'duplicate {s!r} entry at XSDT+{k:#x}')
            if s not in (b'FACP', b'APIC'):
                viol('POINTER', f'XSDT+{k:#x} -> {p:#x}: signature {s!r} is not a table of the set')
                header(p, s, f'XSDT entry +{k:#x}')
            sigs[k] = s
        names = sorted(s.decode('latin1') for s in sigs.values())
        if names != ['APIC', 'FACP']:
            viol('XSDT', f'tables {names}; CLM-03-03-69 builds exactly FACP and APIC here (DSDT/FACS via FADT); '
                         'no HPET/MCFG/EFI tables')
    ptr = {s: u64(xs, k) for k, s in sigs.items()} if xs else {}

    # ---- FADT ------------------------------------------------------------------------------------------------------
    fa = header(ptr[b'FACP'], b'FACP', 'FADT') if b'FACP' in ptr else None
    if xs and b'FACP' not in ptr:
        viol('XSDT', 'no FADT')
    facs = dsdt = None
    if fa:
        if len(fa) != 276 or fa[8] != 6 or (fa[131] & 15) != 5:
            viol('REVISION', f'FADT length {len(fa)} major {fa[8]} minor {fa[131]:#x}; ACPI 6.5 is 276 bytes, 6.5')
        nf('FADT minor errata', fa[131] >> 4, 'errata level of the minor version', 'ok')
        if len(fa) >= 244:
            flags = u32(fa, 112)
            for field, lo, hi in (('FIRMWARE_CTRL', u32(fa, 36), u64(fa, 132)), ('DSDT', u32(fa, 40), u64(fa, 140))):
                if lo == 0 and hi == 0:
                    viol('FADT', f'{field} and X_{field} both zero')
                elif lo and hi and lo != hi:
                    viol('FADT', f'{field} {lo:#x} and X_{field} {hi:#x} disagree')
            fp = u64(fa, 132) or u32(fa, 36)
            dp = u64(fa, 140) or u32(fa, 40)
            nf('FADT FIRMWARE_CTRL / X_FIRMWARE_CTRL', f'{u32(fa, 36):#x} / {u64(fa, 132):#x}',
               'X_ is preferred when nonzero (ACPI 6.5 Table 5.9); both set and equal is tolerated', 'ok')
            nf('FADT DSDT / X_DSDT', f'{u32(fa, 40):#x} / {u64(fa, 140):#x}', 'X_ preferred when nonzero', 'ok')
            facs_b = img.read(fp, 64)
            if facs_b[:4] != b'FACS':
                viol('POINTER', f'FACS at {fp:#x}: signature {facs_b[:4]!r}')
            else:
                ln = u32(facs_b, 4)
                facs = img.read(fp, max(ln, 64))
                tables['FACS'] = (fp, ln)
                info['FACS'] = dict(gpa=hex(fp), length=ln, version=facs[32])
                if ln != 64:
                    viol('FACS', f'length {ln} (64)')
                if fp % 64:
                    viol('FACS', f'at {fp:#x}: not 64-byte aligned (ACPI 6.5 §5.2.10)')
                if not img.written(fp, ln):
                    viol('POINTER', f'FACS at {fp:#x}+{ln:#x}: not written by the loader')
                rec = img.extent_at(fp)
                if rec is not None and rec != ln:
                    viol('LENGTH', f'FACS at {fp:#x}: length {ln:#x}, write record {rec:#x}')
                if facs[32] not in (2, 3):
                    viol('REVISION', f'FACS version {facs[32]} (2 or 3)')
                nf('FACS version', facs[32], 'the 6.6 snapshot gives 3; the 6.5 value is not in the repo snapshot',
                   'judge: 2 if ACPI 6.5 Table 5.13 still says 2')
                if u32(facs, 12) or u64(facs, 24) or u32(facs, 16) or u32(facs, 36) or any(facs[33:36]) \
                        or any(facs[40:64]):
                    viol('FACS', 'waking vectors, global lock, OSPM flags and reserved bytes must be zero at hand-off')
                ff = u32(facs, 20)
                if ff & 1:
                    viol('SLEEP', 'FACS S4BIOS_F set: S4 is not supported (CLM-03-03-22/69)')
                if ff & ~3:
                    viol('FACS', f'flags {ff:#x}: reserved bits')
                nf('FACS hardware signature', hex(u32(facs, 8)), 'changes signal a hardware change across S4', 'ok')
                nf('FACS 64BIT_WAKE_SUPPORTED_F', ff >> 1 & 1, '64-bit wake environment supported (S3/S4 only)',
                   'ok' if not ff & 2 else 'FLAG: describes a wake path the machine has no S3/S4 for')
            dsdt = header(dp, b'DSDT', 'DSDT')

            # fixed fields
            if fa[44] not in (0, 1):
                viol('FADT', f'reserved (INT_MODEL) byte {fa[44]}')
            nf('FADT reserved byte 44 (ACPI 1.0 INT_MODEL)', fa[44], '0 preferred, 1 allowed for ACPI 1.0',
               'ok (QEMU: 1, multiple APIC)' if fa[44] == QEMU_INT_MODEL else
               'FLAG: QEMU sets 1 (multiple APIC); a deviation needs the builder author\'s record')
            prof_names = ['Unspecified', 'Desktop', 'Mobile', 'Workstation', 'Enterprise Server', 'SOHO Server',
                          'Appliance PC', 'Performance Server', 'Tablet']
            nf('FADT Preferred_PM_Profile', fa[45], prof_names[fa[45]] if fa[45] < 9 else 'reserved',
               'ok' if fa[45] < 9 else 'FLAG: reserved value')
            if u16(fa, 46) != SCI:
                viol('ROUTE', f'SCI_INT {u16(fa, 46)} (CLM-03-03-23/69: 9)')
            if u32(fa, 48) or fa[52] or fa[53]:
                viol('FADT', f'SMI_CMD {u32(fa, 48):#x} ACPI_ENABLE {fa[52]:#x} ACPI_DISABLE {fa[53]:#x}: '
                             'CLM-03-03-69 is ACPI-only mode, zero SMI enable/disable command')
            if fa[54]:
                viol('SLEEP', f'S4BIOS_REQ {fa[54]:#x}: no S4')
            if fa[55]:
                viol('FADT', f'PSTATE_CNT {fa[55]:#x} with no SMI command')
            if fa[95]:
                viol('IDLE', f'CST_CNT {fa[95]:#x}: advertises _CST support (plan §1a: no _CST)')
            blocks = [('PM1a_EVT_BLK', 56, 88, 148, PMBASE, 4), ('PM1b_EVT_BLK', 60, 88, 160, 0, None),
                      ('PM1a_CNT_BLK', 64, 89, 172, PMBASE + 4, 2), ('PM1b_CNT_BLK', 68, 89, 184, 0, None),
                      ('PM2_CNT_BLK', 72, 90, 196, 0, 0), ('PM_TMR_BLK', 76, 91, 208, PMBASE + 8, 4),
                      ('GPE0_BLK', 80, 92, 220, 'gpe0', GPE0_LEN), ('GPE1_BLK', 84, 93, 232, 0, 0)]
            for name, lo_o, len_o, x_o, want, want_len in blocks:
                lo, ln, xg = u32(fa, lo_o), fa[len_o], gas(fa, x_o)
                addr = xg['addr'] or lo
                if xg['addr']:
                    if xg['space'] != 1 or xg['offset'] or xg['access'] > 4:
                        viol('GAS', f'X_{name} at FADT+{x_o}: {fmt_gas(xg)} (SystemIO, offset 0, access 0..4)')
                    if lo and lo != xg['addr']:
                        viol('FADT', f'{name} {lo:#x} vs X_{name} address {xg["addr"]:#x}')
                    if 'b' not in name and xg['width'] != 8 * ln:
                        viol('FADT', f'X_{name} width {xg["width"]} vs legacy length {ln}')
                elif any(fa[x_o:x_o + 12]):
                    viol('GAS', f'X_{name} at FADT+{x_o}: address zero but other bytes set')
                if want == 'gpe0':
                    if addr == 0:
                        nf('FADT GPE0_BLK', 'absent', 'no general-purpose event block',
                           'ok: omitted by decision (main session); the model still decodes 0xafe0..0xafe3')
                    else:
                        if addr != GPE0 or ln != GPE0_LEN:
                            viol('FADT', f'GPE0_BLK {addr:#x} length {ln} (CLM-03-03-69: 0xafe0..0xafe3)')
                        nf('FADT GPE0_BLK', f'{addr:#x} len {ln}', 'GPE0 status (2 bytes) + enable (2 bytes); '
                           'no _GPE methods or GPE devices are needed for it', 'ok')
                elif name.startswith(('PM1b', 'PM2', 'GPE1')):
                    if addr:
                        viol('FADT', f'{name} {addr:#x}: no such block on the PIIX4 PM function')
                elif addr != want or ln != want_len:
                    viol('FADT', f'{name} {addr:#x} length {ln} (PIIX4 PM at PMBASE 0xb000: '
                                 f'{want:#x} length {want_len})')
            if fa[94]:
                nf('FADT GPE1_BASE', fa[94], 'GPE1 numbering base with no GPE1 block', 'FLAG: nonzero without GPE1')
            p2, p3 = u16(fa, 96), u16(fa, 98)
            if p2 <= 100 or p3 <= 1000:
                viol('IDLE', f'P_LVL2_LAT {p2} / P_LVL3_LAT {p3}: values <= 100 / <= 1000 advertise C2/C3 '
                             '(plan §1a/§1e: only C1/HLT)')
            nf('FADT P_LVL2_LAT / P_LVL3_LAT', f'{p2} / {p3}', '>100 / >1000 means C2/C3 unsupported', 'ok')
            nf('FADT FLUSH_SIZE / FLUSH_STRIDE', f'{u16(fa, 100)} / {u16(fa, 102)}',
               'cache flush parameters, used only when WBINVD is not set', 'ok')
            nf('FADT DUTY_OFFSET / DUTY_WIDTH', f'{fa[104]} / {fa[105]}', 'throttling field in P_CNT (P_BLK)',
               'ok' if fa[105] == 0 else 'FLAG: throttling width with no P_BLK')
            if fa[108] != 0x32:
                viol('FADT', f'CENTURY {fa[108]:#x} at FADT+108: the reused RTC keeps the century at CMOS 0x32')
            nf('FADT DAY_ALRM / MON_ALRM', f'{fa[106]:#x} / {fa[107]:#x}',
               'RTC CMOS indices of day/month alarm (0 = not supported)',
               'ok (QEMU: 0 / 0)' if fa[106] == fa[107] == 0
               else 'judge: alarm indices must exist in the reused RTC model\'s CMOS')
            ia = u16(fa, 109)
            dec = [IAPC[k] for k in range(6) if ia >> k & 1]
            j = []
            if ia & 2:
                viol('FADT', f'IAPC_BOOT_ARCH {ia:#x} at FADT+109 claims an 8042; the device set has none')
            if not ia & 4:
                j.append('VGA not marked absent: Linux may probe legacy VGA')
            if not ia & 1:
                j.append('LEGACY_DEVICES clear although ISA devices (COM1, PIC, PIT, RTC) exist')
            if ia & 0x20:
                viol('FADT', 'IAPC_BOOT_ARCH CMOS_RTC_NOT_PRESENT: the RTC is part of the legacy island (§1a)')
            if ia & ~0x3F:
                viol('FADT', f'IAPC_BOOT_ARCH reserved bits {ia:#x}')
            nf('FADT IAPC_BOOT_ARCH', f'{ia:#x} {dec}', 'IA-PC boot architecture flags',
               'FLAG: ' + '; '.join(j) if j else 'ok')
            if fa[111]:
                viol('FADT', f'reserved byte 111 = {fa[111]:#x}')
            if flags & (1 << 20):
                viol('FADT', 'HW_REDUCED_ACPI set: the machine has the conventional PM register interface')
            if not flags & (1 << 10):
                viol('RESET', 'RESET_REG_SUP clear (CLM-03-03-69)')
            if flags >> 24 or (flags >> 22 & 3) == 3:
                viol('FADT', f'Flags {flags:#x}: reserved bits/values')
            if flags >> 8 & 1:
                viol('FADT', 'TMR_VAL_EXT set (Flags bit 8, FADT+113): the reused PIIX4 PM timer is 24-bit')
            if not flags >> 2 & 1:
                viol('FADT', 'PROC_C1 clear (Flags bit 2, FADT+112): C1/HLT is the one idle state')
            for k, nm in enumerate(FADT_FLAGS):
                if nm in ('HW_REDUCED_ACPI', 'RESET_REG_SUP', 'TMR_VAL_EXT', 'PROC_C1'):
                    continue
                v, q = flags >> k & 1, qemu_flags(n) >> k & 1
                judge = f'ok (QEMU: {q})' if v == q else \
                    f'FLAG: QEMU sets {q}; a deviation needs the builder author\'s record'
                if nm == 'PWR_BUTTON' and v == q == 0:
                    judge += '; fixed power button: PM1 PWRBTN of the reused model (no host source named yet)'
                if nm == 'RTC_S4' and v == q == 1:
                    judge += '; QEMU sets it although no S4 is advertised (harmless)'
                nf(f'FADT flag {nm}', v, f'bit {k}', judge)
            nf('FADT flag PERSISTENT_CPU_CACHES', flags >> 22 & 3, 'bits 22-23', 'ok')
            rg = gas(fa, 116)
            if (rg['space'], rg['width'], rg['offset'], rg['addr']) != (1, 8, 0, RESET_PORT) or rg['access'] not in (0, 1):
                viol('RESET', f'RESET_REG {fmt_gas(rg)} (CLM-03-03-69: SystemIO, 8 bits, offset 0, 0xb040)')
            if fa[128] != RESET_VALUE:
                viol('RESET', f'RESET_VALUE {fa[128]:#x} (CLM-03-03-69: 0x06)')
            nf('FADT RESET_REG access size', rg['access'], '0 undefined (legacy), 1 byte', 'ok')
            if u16(fa, 129):
                viol('FADT', f'ARM_BOOT_ARCH {u16(fa, 129):#x}: must be 0 on IA-PC')
            if len(fa) >= 276:
                for nm, o in (('SLEEP_CONTROL_REG', 244), ('SLEEP_STATUS_REG', 256)):
                    if any(fa[o:o + 12]):
                        viol('FADT', f'{nm} {fmt_gas(gas(fa, o))}: hardware-reduced only')
                nf('FADT Hypervisor Vendor Identity', hex(u64(fa, 268)), 'identifies the hypervisor (informational)',
                   'ok')

    # ---- MADT ------------------------------------------------------------------------------------------------------
    ma = header(ptr[b'APIC'], b'APIC', 'MADT') if b'APIC' in ptr else None
    if xs and b'APIC' not in ptr:
        viol('XSDT', 'no MADT')
    procs, wake, isos, ioapics, nmis = [], [], [], [], []
    if ma:
        if ma[8] not in (5, 6):
            viol('REVISION', f'MADT revision {ma[8]}: the version-0 wakeup structure is ACPI 6.4/6.5 (revision 5/6)')
        nf('MADT revision', ma[8], 'the 6.6 snapshot gives 7; 6.4 is 5; 6.5 not in the repo snapshot',
           'judge: confirm against ACPI 6.5 Table 5.19')
        base = prof.msr(0x1B)
        want_base = int(base['reset'], 0) & ~0xFFF
        if u32(ma, 36) != want_base:
            viol('MADT', f'Local Interrupt Controller Address {u32(ma, 36):#x} vs IA32_APIC_BASE reset {want_base:#x}')
        if not u32(ma, 40) & 1:
            viol('MADT', 'PCAT_COMPAT clear although the 8259 PIC is present (CLM-03-03-23)')
        if u32(ma, 40) & ~1:
            viol('MADT', f'flags {u32(ma, 40):#x}: reserved bits')
        o = 44
        while o < len(ma):
            if o + 2 > len(ma):
                viol('MADT', f'structure header at MADT+{o:#x} crosses the table end')
                break
            t, ln = ma[o], ma[o + 1]
            if t not in MADT_LEN:
                viol('MADT', f'structure type {t:#x} at MADT+{o:#x}: not an x86 interrupt controller structure')
            elif ln != MADT_LEN[t] and not (t == 0x10 and ln == 24):
                viol('MADT', f'type {t:#x} at MADT+{o:#x}: length {ln}, expected {MADT_LEN[t]}')
            if ln < 2 or o + ln > len(ma):
                viol('MADT', f'type {t:#x} at MADT+{o:#x} length {ln} runs past the table end {len(ma):#x}')
                break
            s = ma[o:o + ln]
            if t == 0 and ln == 8:
                procs.append(dict(kind='LAPIC', uid=s[2], apic=s[3], flags=u32(s, 4), off=o))
            elif t == 9 and ln == 16:
                procs.append(dict(kind='x2APIC', uid=u32(s, 12), apic=u32(s, 4), flags=u32(s, 8), off=o))
                if u16(s, 2):
                    viol('MADT', f'x2APIC at MADT+{o:#x}: reserved bytes')
            elif t == 1 and ln == 12:
                ioapics.append(dict(id=s[2], addr=u32(s, 4), gsi=u32(s, 8), off=o))
                if s[3]:
                    viol('MADT', f'IOAPIC at MADT+{o:#x}: reserved byte')
            elif t == 2 and ln == 10:
                isos.append(dict(bus=s[2], src=s[3], gsi=u32(s, 4), flags=u16(s, 8), off=o))
            elif t in (4, 0xA, 3):
                nmis.append((t, s.hex(), o))
            elif t == 5:
                viol('MADT', f'LAPIC address override at MADT+{o:#x}: the base fits 32 bits')
            elif t == 0x10:
                wake.append(dict(version=u16(s, 2), reserved=u32(s, 4), mailbox=u64(s, 8), len=ln, off=o))
            o += ln
        info['MADT_structures'] = dict(procs=procs, ioapics=ioapics, isos=isos, wake=wake, nmis=nmis)

        # processors vs CPUID (CLM-03-03-69: APIC IDs 0..N-1 across CPUID, MADT, mailbox, LAPIC; no spare CPUs)
        want = []
        for v in range(n):
            i1 = prof.cpuid(1, 0, n=n, vcpu=v)[1] >> 24
            xb = prof.cpuid(0xB, 0, n=n, vcpu=v)[3]
            if i1 != xb & 0xFF:
                viol('MADT-APIC', f'profile: vCPU {v} initial APIC ID {i1} vs x2APIC ID {xb}')
            want.append(xb)
        got = [p['apic'] for p in procs]
        if sorted(got) != sorted(want) or len(set(got)) != len(got):
            viol('MADT-APIC', f'N={n}: processor APIC IDs {got}, CPUID gives {want}')
        for p in procs:
            if p['flags'] != 1:
                viol('MADT-APIC', f'{p["kind"]} APIC ID {p["apic"]} at MADT+{p["off"]:#x}: flags {p["flags"]:#x} '
                                  '(enabled, online-capable must be 0 when enabled, reserved 0)')
            if p['kind'] == 'x2APIC' and p['apic'] < 0xFF:
                viol('MADT-APIC', f'x2APIC structure for APIC ID {p["apic"]} < 255 at MADT+{p["off"]:#x}: '
                                  'ACPI requires the Processor Local APIC structure (Linux ignores it beside LAPICs)')
            if p['kind'] == 'LAPIC' and p['uid'] == 0xFF:
                viol('MADT-APIC', f'LAPIC at MADT+{p["off"]:#x}: UID 0xff is the all-processors value')
        uids = [p['uid'] for p in procs]
        if len(set(uids)) != len(uids):
            viol('UID', f'duplicate processor UIDs {uids}')
        if procs and procs[0]['apic'] != want[0]:
            nf('MADT first processor entry', procs[0]['apic'], 'ACPI: the boot processor should be listed first',
               'FLAG: not the BSP')
        x2 = prof.feature('1.ecx.X2APIC')
        nf('MADT processor structure kind', sorted({p['kind'] for p in procs}),
           f'APIC IDs < 255 use type 0 even with x2APIC advertised (profile x2APIC={x2})', 'ok')

        if len(ioapics) != 1 or ioapics[0]['addr'] != IOAPIC_BASE or ioapics[0]['gsi'] != 0:
            viol('MADT', f'IOAPICs {ioapics} (CLM-03-03-69: one at 0xfec00000, GSI base 0)')
        for io in ioapics:
            nf('MADT IOAPIC ID', io['id'], 'IOAPIC ID (QEMU uses 0)',
               'ok' if io['id'] not in want else 'judge: equals a LAPIC ID (allowed for xAPIC, separate namespaces)')

        # interrupt routing (CLM-03-03-23): IRQ0 -> GSI2 edge/high; SCI 9 level/low; others identity
        seen = {}
        for s in isos:
            pol, trig = s['flags'] & 3, s['flags'] >> 2 & 3
            if s['bus'] != 0:
                viol('ROUTE', f'override at MADT+{s["off"]:#x}: bus {s["bus"]} (ISA is 0)')
            if s['flags'] & ~0xF or pol == 2 or trig == 2:
                viol('ROUTE', f'override at MADT+{s["off"]:#x}: MPS INTI flags {s["flags"]:#x} reserved')
            if s['src'] in seen:
                viol('ROUTE', f'two overrides for IRQ {s["src"]}')
            seen[s['src']] = s
            if s['src'] == 0:
                if s['gsi'] != 2 or pol not in (0, 1) or trig not in (0, 1):
                    viol('ROUTE', f'IRQ0 override GSI {s["gsi"]} flags {s["flags"]:#x} (GSI 2, edge/high)')
                nf('MADT IRQ0 override flags', hex(s['flags']), 'conforms to ISA (edge, active high)' if s['flags'] == 0
                   else f'polarity {pol} trigger {trig}', 'ok')
            elif s['src'] == SCI:
                if s['gsi'] != SCI or s['flags'] != SCI_FLAGS:
                    viol('ROUTE', f'IRQ9 (SCI) override at MADT+{s["off"]:#x}: GSI {s["gsi"]} flags {s["flags"]:#06x} '
                                  '(decision: GSI 9, flags 0x000d level/active high)')
            elif s['src'] in (4, 8):
                if s['gsi'] != s['src'] or pol not in (0, 1) or trig not in (0, 1):
                    viol('ROUTE', f'IRQ{s["src"]} (COM1/RTC) override to GSI {s["gsi"]} flags {s["flags"]:#x}')
            else:
                nf(f'MADT override IRQ {s["src"]}', f'GSI {s["gsi"]} flags {s["flags"]:#x}',
                   'interrupt source override', 'FLAG: no device of the records uses this IRQ')
        if 0 not in seen:
            viol('ROUTE', 'no IRQ0 -> GSI2 override (CLM-03-03-23/69)')
        # Main-session decision (supersedes claim 69's level/low): an explicit IRQ9 -> GSI9 level/high override.
        if SCI not in seen:
            viol('ROUTE', 'no IRQ9 (SCI) override; required: GSI 9, flags 0x000d (level, active high)')
        uidset = {p['uid'] for p in procs}
        if not any(t in (4, 0xA) for t, _, _ in nmis):
            viol('MADT-NMI', 'no local APIC NMI structure (LINT1 NMI, as QEMU)')
        for t, raw, o in nmis:
            b = bytes.fromhex(raw)
            if t in (4, 0xA):
                uid, fl, lint = (b[2], u16(b, 3), b[5]) if t == 4 else (u32(b, 4), u16(b, 2), b[8])
                if lint != 1:
                    viol('MADT-NMI', f'local APIC NMI at MADT+{o:#x}: LINT{lint} (LINT1)')
                if uid not in uidset and uid != (0xFF if t == 4 else 0xFFFFFFFF):
                    viol('MADT-NMI', f'local APIC NMI at MADT+{o:#x}: UID {uid:#x} names no processor')
                nf(f'MADT local APIC NMI at +{o:#x}', raw, f'UID {uid:#x}, MPS flags {fl:#x}, LINT{lint}',
                   'ok (QEMU: all processors, flags 0, LINT1)' if (uid, fl) in ((0xFF, 0), (0xFFFFFFFF, 0))
                   else 'judge: QEMU uses UID 0xff and flags 0')
            else:
                nf(f'MADT NMI source at +{o:#x}', raw, 'NMI source structure', 'FLAG: QEMU emits none')

        # mailbox (ACPI 6.5 §5.2.12.19 version 0; CLM-03-03-69)
        if len(wake) != 1:
            viol('MADT-WAKEUP', f'{len(wake)} multiprocessor wakeup structures (exactly one)')
        for w in wake:
            if w['len'] != 16 or w['version'] != 0:
                viol('MADT-WAKEUP', f'at MADT+{w["off"]:#x}: length {w["len"]} version {w["version"]} '
                                    '(version 0, 16 bytes)')
            if w['reserved']:
                viol('MADT-WAKEUP', f'at MADT+{w["off"]:#x}: reserved {w["reserved"]:#x}')
            mb = w['mailbox']
            if mb % 4096:
                viol('MAILBOX', f'mailbox {mb:#x} not 4 KiB aligned')
            r = e820_of(mb, 4096)
            if not r or r[2] != 4 or not (NVS[0] <= mb and mb + 4096 <= NVS[1]):
                viol('MAILBOX', f'mailbox {mb:#x}+0x1000 not inside the ACPI NVS e820 extent '
                                f'{NVS[0]:#x}..{NVS[1]:#x} (e820 entry {r})')
            m = img.read(mb, 16)
            if u16(m, 0) or u16(m, 2):
                viol('MAILBOX', f'command {u16(m, 0)} reserved {u16(m, 2)}: must be Noop at hand-off')
            nf('Mailbox OS/firmware sections', 'zero' if not any(img.read(mb, 4096)) else 'nonzero',
               'ApicId/WakeupVector/OS/firmware sections at hand-off', 'ok' if not any(img.read(mb, 4096))
               else 'judge: firmware section content')
            tables['mailbox'] = (mb, 4096)

    # ---- placement -------------------------------------------------------------------------------------------------
    for name, (a, ln) in tables.items():
        r = e820_of(a, ln)
        if not r or r[2] not in (3, 4):
            viol('PLACEMENT', f'{name} {a:#x}+{ln:#x}: not inside one ACPI (3) or NVS (4) e820 extent ({r})')
        if not (NVS[0] <= a and a + ln <= NVS[1]):
            viol('PLACEMENT', f'{name} {a:#x}+{ln:#x}: outside the CLM-03-03-69 ACPI NVS range 0x600000..0x800000')
    items = sorted(tables.items(), key=lambda kv: kv[1])
    for (n1, (a1, l1)), (n2, (a2, l2)) in zip(items, items[1:]):
        if a1 + l1 > a2:
            viol('OVERLAP', f'{n1} {a1:#x}+{l1:#x} overlaps {n2} {a2:#x}+{l2:#x}')
    for name, a in (('IOAPIC', IOAPIC_BASE), ('LAPIC', LAPIC_BASE)):
        r = e820_of(a, 4096)
        if not r or r[2] != 2:
            viol('PLACEMENT', f'{name} MMIO {a:#x}: not inside a reserved e820 extent ({r})')
        if any(s < a + 4096 and a < s + size and t == 1 for s, size, t in e820):
            viol('PLACEMENT', f'{name} MMIO {a:#x} overlaps e820 RAM')

    # ---- DSDT ------------------------------------------------------------------------------------------------------
    listing = ''
    if dsdt:
        if dsdt[8] != 2:
            viol('REVISION', f'DSDT revision {dsdt[8]} (2: 64-bit AML integers)')
        aml = Aml(dsdt[36:], tables['DSDT'][0] + 36)
        try:
            aml.termlist(0, len(dsdt) - 36, '\\', 0)
        except AmlError as e:
            viol('AML', f'DSDT: {e}')
        hdr = (f'/* DSDT at {tables["DSDT"][0]:#x}, length {len(dsdt):#x}, revision {dsdt[8]}, '
               f'OEM {info["DSDT"]["oem"]!r} {info["DSDT"]["oem_table"]!r} */')
        listing = '\n'.join([hdr, 'DefinitionBlock ("", "DSDT", 2, ...)', '{'] + aml.lines + ['}'])
        obj = aml.objects
        segs = lambda p: p.rsplit('.', 1)[-1].lstrip('\\')         # noqa: E731
        for p, (kind, val, off) in obj.items():
            if segs(p) in ('_S1_', '_S2_', '_S3_', '_S4_'):
                viol('SLEEP', f'{p} at gpa {aml.gpa + off:#x}: S1-S4 must not be advertised (CLM-03-03-22/69)')
            if segs(p) == '_CST':
                viol('IDLE', f'{p} at gpa {aml.gpa + off:#x}: no _CST (plan §1a)')
            if kind == 'Processor' and (val[1] or val[2]):
                viol('IDLE', f'Processor {p} P_BLK {val[1]:#x} length {val[2]}: no P_BLK (plan §1a)')
        s5 = obj.get('\\_S5_')
        if not s5:
            viol('SLEEP', 'no \\_S5_ object (CLM-03-03-22)')
        elif s5[0] != 'Name' or not isinstance(s5[1], tuple) or s5[1][0] != 'package' or not s5[1][2] \
                or not all(isinstance(x, int) for x in s5[1][2]):
            viol('SLEEP', f'\\_S5_ is not a package of integers: {s5[:2]}')
        else:
            el = s5[1][2]
            if el[0] != 0:
                viol('SLEEP', f'\\_S5_ SLP_TYPa {el[0]} (CLM-03-03-22: PIIX4 PM1 sleep type 0 is S5)')
            nf('\\_S5_ package', el, 'SLP_TYPa, SLP_TYPb, reserved...; SLP_TYPb unused without PM1b',
               'ok' if len(el) >= 2 and el[1] == 0 else 'judge: SLP_TYPb')
        devices = {}
        for p, (kind, val, off) in obj.items():
            if kind in ('Device', 'Processor'):
                hid = obj.get(p + '._HID')
                h = hid and hid[1]
                h = eisaid(h) if isinstance(h, int) else h
                uid = obj.get(p + '._UID')
                devices[p] = dict(kind=kind, hid=h, uid=uid and uid[1], val=val, off=off)
        info['DSDT_devices'] = {p: {k: v for k, v in d.items() if k != 'off'} for p, d in devices.items()}
        # ISA devices: COM1 (CLM-03-03-69: PNP0501, 0x3f8..0x3ff, IRQ4); RTC (main-session decision: PNP0B00,
        # 0x70 length 2, IRQ8). Each _CRS: exactly one I/O range and one edge/high interrupt.
        isa = []
        for hid, label, port, plen, irqn in (('PNP0501', 'COM1', 0x3F8, 8, 4), ('PNP0B00', 'RTC', 0x70, 2, 8)):
            found = [p for p, d in devices.items() if d['hid'] == hid]
            isa += found
            if len(found) != 1:
                viol('DEVICES', f'{hid} devices {found} (exactly one {label})')
            for p in found:
                crs = obj.get(p + '._CRS')
                if not crs or crs[0] != 'Name' or not isinstance(crs[1], tuple) or crs[1][0] != 'buffer':
                    viol('CRS', f'{p}._CRS missing or not a static buffer')
                    continue
                size, init = crs[1][1], crs[1][2]
                if size != len(init):
                    viol('CRS', f'{p}._CRS BufferSize {size} vs {len(init)} initializer bytes')
                its, probs = decode_resources(init)
                for pr in probs:
                    viol('CRS', f'{p}._CRS: {pr}')
                info[f'{label}_CRS'] = its
                ios = [x for x in its if x['type'] in ('IO', 'FixedIO')]
                irq = [x for x in its if x['type'] in ('IRQ', 'Interrupt')]
                ok_io = len(ios) == 1 and ((ios[0].get('min'), ios[0].get('max'), ios[0]['len']) == (port, port, plen)
                                           or (ios[0].get('base'), ios[0]['len']) == (port, plen))
                if not ok_io:
                    viol('DEVICES', f'{p}._CRS I/O {ios} ({label}: {port:#x} length {plen})')
                ok_irq = len(irq) == 1 and (irq[0].get('irqs') == [irqn] and irq[0]['edge'] and not irq[0]['low']
                                            or irq[0].get('gsis') == [irqn] and not irq[0]['level']
                                            and not irq[0]['low'])
                if not ok_irq:
                    viol('DEVICES', f'{p}._CRS interrupt {irq} ({label}: IRQ{irqn}, ISA edge/high)')
                if len(its) != len(ios) + len(irq) + 1:
                    viol('CRS', f'{p}._CRS has other descriptors: {[x["type"] for x in its]}')
                if ios and ios[0].get('align') is not None:
                    nf(f'{p}._CRS I/O alignment', ios[0]['align'], 'IO descriptor alignment (min = max, unused)',
                       'ok')
                nf(f'{p}._UID', devices[p]['uid'], f'{label} instance', 'ok')
        com = isa
        # processors (UIDs vs MADT)
        cpus = {p: d for p, d in devices.items() if d['kind'] == 'Processor' or d['hid'] == 'ACPI0007'}
        dsdt_uids = []
        for p, d in cpus.items():
            if d['kind'] == 'Processor':
                dsdt_uids.append(d['val'][0])
            elif not isinstance(d['uid'], int):
                viol('UID', f'{p}: ACPI0007 device without an integer _UID ({d["uid"]!r})')
            else:
                dsdt_uids.append(d['uid'])
        if ma and sorted(dsdt_uids) != sorted(p['uid'] for p in procs):
            viol('UID', f'DSDT processor UIDs {sorted(dsdt_uids)} vs MADT {sorted(p["uid"] for p in procs)}')
        if len(set(dsdt_uids)) != len(dsdt_uids):
            viol('UID', f'duplicate DSDT processor UIDs {dsdt_uids}')
        for p, d in devices.items():
            if d['hid'] in ('PNP0103', 'PNP0C0C', 'PNP0C0E', 'PNP0303', 'PNP0F13', 'PNP0A03', 'PNP0A08', 'PNP0C0A',
                            'ACPI0013', 'PNP0C80'):
                viol('DEVICES', f'{p} {d["hid"]}: a device outside the CLM-03-03-69 set')
        other = {p: d['hid'] for p, d in devices.items() if p not in com and p not in cpus}
        nf('DSDT other devices', other or 'none', 'devices besides COM1, the RTC and processors',
           'FLAG: CLM-03-03-69 also names LNRO0005 virtio-mmio resources; none described (later chunk?)'
           if not any(d == 'LNRO0005' for d in other.values()) else 'judge: check against claim 69 windows/GSIs')
        nf('DSDT processor objects', sorted(cpus), 'ACPI0007 devices or Processor objects', 'ok')
    return dict(violations=V, not_fixed=NF, listing=listing, info=info)

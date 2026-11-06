"""Independent reference checker for the x86-64 Linux boot protocol (loader side).

Derived only from the published protocol, not from natemu code or one kernel's quirks:
  B  = Documentation/arch/x86/boot.rst            (v6.12.107)
  Z  = Documentation/arch/x86/zero-page.rst       (v6.12.107)
  BP = arch/x86/include/uapi/asm/bootparam.h      (v6.12.107)
  E8 = arch/x86/include/asm/e820/types.h          (v6.12.107)
Upstream copies: scratchpad/bootproto/ (gregkh/linux tag v6.12.107).
Scope: 64-bit boot protocol only (32-bit and real-mode entry are deferred by owner decision).

  parse_bzimage(bytes) -> BzImage          raises BzImageError(code, detail, cite)
  check_loaded(memory, rip, rsi, regs, layout) -> [Violation(rule, detail, cite)]   ([] = pass)

`memory`: object with read(addr, n) -> bytes (raise on unmapped), or a flat bytes/bytearray from 0.
`regs`: dict with cr0, cr3, cr4, efer, rflags, cs, ds, es, ss, gdtr_base, gdtr_limit.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

PAGE = 4096
BOOT_CS, BOOT_DS = 0x10, 0x18
LOADED_HIGH = 0x01
XLF_KERNEL_64, XLF_ABOVE_4G = 0x01, 0x02
E820_RAM, E820_RESERVED, E820_ACPI, E820_NVS = 1, 2, 3, 4
E820_TYPES = {1, 2, 3, 4, 5, 7, 12, 128, 0xEFFFFFFF}  # E8 enum e820_type
SETUP_DATA_MAX_NODES = 64  # bound for the list walk (checker's own limit, not the spec's)
E820_MAX = 128  # BP E820_MAX_ENTRIES_ZEROPAGE
MIN_VERSION = 0x020C  # B "Protocol 2.12 ... xloadflags ... above 4G in 64bit"
KERNEL_NAMES = ("kernel", "zero page", "cmdline", "initrd")

# ---- citations (exact text) ----------------------------------------------------------------
B64 = 'B "64-bit Boot Protocol"'
C_MODE = B64 + ': "At entry, the CPU must be in 64-bit mode with paging enabled."'
C_IDENT = (B64 + ': "The range with setup_header.init_size from start address of loaded kernel and '
           'zero page and command line buffer get ident mapping;"')
C_GDT = (B64 + ': "a GDT must be loaded with the descriptors for selectors __BOOT_CS(0x10) and '
         '__BOOT_DS(0x18); both descriptors must be 4G flat segment;')
C_CS = B64 + ': "__BOOT_CS must have execute/read permission,"'
C_DS = B64 + ': "__BOOT_DS must have read/write permission;"'
C_SEL = B64 + ': "CS must be __BOOT_CS and DS, ES, SS must be __BOOT_DS;"'
C_IF = B64 + ': "interrupt must be disabled;"'
C_RSI = B64 + ': "%rsi must hold the base address of the struct boot_params."'
C_ENTRY = (B64 + ': "the kernel is started by jumping to the 64-bit kernel entry point, which is the '
           'start address of loaded 64-bit kernel plus 0x200."')
C_ZERO = (B64 + ': "struct boot_params ... initialized to all zero. Then, the setup header at offset '
          '0x01f1 of kernel image on should be loaded into struct boot_params"')
C_SENT = 'BP struct boot_params.sentinel: "kernel should zero out certain portions of boot_params" if still 0xff'
C_LOADER = ('B field type_of_loader (write, obligatory): "enter 0xTV here ... Otherwise, enter 0xFF here." '
            '(0x00 is "reserved for pre-2.00 bootloader")')
C_CMDPTR = ('B field cmd_line_ptr (write, obligatory): "If this field is left at zero, the kernel will '
            'assume that your boot loader does not support the 2.02+ protocol."')
C_CMDSZ = ('B field cmdline_size: "The maximum size of the command line without the terminating zero. '
           'This means that the command line can contain at most cmdline_size characters."')
C_INITRD0 = 'B field ramdisk_image: "Leave at zero if there is no initial ramdisk/ramfs."'
C_INITRD = ('B field ramdisk_image/ramdisk_size (write, obligatory); Z 0C0/004 ext_ramdisk_image '
            '"ramdisk_image high 32bits", 0C4/004 ext_ramdisk_size')
C_IMAX = ('B field initrd_addr_max: "The maximum address that may be occupied by the initial '
          'ramdisk/ramfs contents." ("the address of the highest safe byte")')
C_4G = 'B field xloadflags bit 1 XLF_CAN_BE_LOADED_ABOVE_4G: "If 1, kernel/boot_params/cmdline/ramdisk can be above 4G."'
C_EXTCMD = 'Z 0C8/004 ext_cmd_line_ptr: "cmd_line_ptr high 32bits"'
C_ALIGN = ('B field kernel_alignment: "Alignment unit required by the kernel (if relocatable_kernel is true.) ... '
           'it is possible for the loader to modify this field to permit a lesser alignment." '
           '+ field min_alignment: "kernel_alignment = 1 << min_alignment"')
C_RUNTIME = ('B field init_size: "if (relocatable_kernel) { if (load_address < pref_address) load_address = '
             'pref_address; runtime_start = align_up(load_address, kernel_alignment); } else { runtime_start = '
             'pref_address; }" ... "memory_window_size = init_size"')
C_PREF = 'B field pref_address: "A relocating bootloader should attempt to load at this address if possible."'
C_C32 = ('B field relocatable_kernel: "After loading, the boot loader must set the code32_start field to '
         'point to the loaded code"')
C_LOAD = ('B "Loading The Rest of The Kernel": "The 32-bit (non-real-mode) kernel starts at offset '
          '(setup_sects+1)*512 in the kernel file ... It should be loaded at ... 0x100000 for bzImage kernels."')
C_E820N = 'Z 1E8/001 e820_entries: "Number of entries in e820_table"; BP E820_MAX_ENTRIES_ZEROPAGE = 128'
C_E820 = 'Z 2D0/A00 e820_table: "E820 memory map table (array of struct e820_entry)"; E8 struct e820_entry'
C_E820T = 'E8 enum e820_type'
C_RSDP = 'Z 070/008 acpi_rsdp_addr: "Physical address of ACPI RSDP table"; BP boot_params.acpi_rsdp_addr @0x070'
C_SETUP = ('B field setup_data (write (special), 2.09+): "The 64-bit physical pointer to NULL terminated single '
           'linked list of struct setup_data." "the next field of the last node is 0"; struct setup_data = '
           '{u64 next; u32 type; u32 len; u8 data[]}')
C_RSDPSIG = 'ACPI 6.5 s5.2.5.3 RSDP: signature "RSD PTR ", bytes 0..19 sum to 0 (not in boot.rst)'


@dataclass(frozen=True)
class Violation:
    rule: str
    detail: str
    cite: str

    def __str__(self):
        return f"{self.rule}: {self.detail} [{self.cite}]"


class BzImageError(Exception):
    def __init__(self, code: str, detail: str, cite: str):
        super().__init__(f"{code}: {detail} [{cite}]")
        self.code, self.detail, self.cite = code, detail, cite


@dataclass(frozen=True)
class BzImage:
    version: int
    setup_sects_raw: int
    setup_sects: int  # effective (0 -> 4)
    syssize: int  # 16-byte paragraphs
    loadflags: int
    loaded_high: bool
    xloadflags: int
    kernel_64: bool
    above_4g: bool
    kernel_alignment: int
    relocatable_kernel: bool
    min_alignment: int
    pref_address: int
    init_size: int
    cmdline_size: int
    initrd_addr_max: int
    handover_offset: int  # informational
    code32_start: int
    payload_offset: int  # from start of protected-mode code; 0 = absent
    payload_length: int
    header_end: int  # 0x202 + byte at 0x201
    pm_offset: int  # file offset of protected-mode code
    pm_size: int  # syssize * 16
    payload_file_offset: int | None
    header: bytes = field(repr=False, default=b"")  # image[0x1f1:header_end]


def _u(d, off, fmt):
    return struct.unpack_from("<" + fmt, d, off)[0]


def parse_bzimage(data: bytes) -> BzImage:
    """Validate the setup header for a 64-bit loader; raise BzImageError otherwise."""
    if len(data) < 0x268:
        raise BzImageError("TRUNCATED_HEADER", f"{len(data)} bytes < 0x268 (header through handover_offset)",
                           'B "The Real-Mode Kernel Header" table, offsets 01F1..0267')
    if _u(data, 0x1FE, "H") != 0xAA55:
        raise BzImageError("BAD_BOOT_FLAG", f"boot_flag {_u(data, 0x1FE, 'H'):#x}", 'B field boot_flag: "Contains 0xAA55."')
    if data[0x202:0x206] != b"HdrS":
        raise BzImageError("BAD_MAGIC", f"header {data[0x202:0x206]!r}", 'B field header: "Contains the magic number HdrS (0x53726448)."')
    version = _u(data, 0x206, "H")
    if version < MIN_VERSION:
        raise BzImageError("VERSION_TOO_OLD", f"protocol {version >> 8}.{version & 0xFF:02d} < 2.12",
                           'B "Protocol 2.12: ... xloadflags field and extension fields ... above 4G in 64bit"')
    if data[0x200] != 0xEB:
        raise BzImageError("BAD_JUMP", f"jump opcode {data[0x200]:#x}", 'B field jump: "0xEB followed by a signed offset relative to byte 0x202"')
    header_end = 0x202 + data[0x201]
    if not (0x268 <= header_end <= 0x290):
        raise BzImageError("BAD_HEADER_END", f"header end {header_end:#x}",
                           'B 64-bit protocol: "0x0202 + byte value at offset 0x0201"; BP hole 0x1f1..0x290')
    loadflags, xloadflags = data[0x211], _u(data, 0x236, "H")
    if not loadflags & LOADED_HIGH:
        raise BzImageError("NOT_LOADED_HIGH", f"loadflags {loadflags:#x}", C_LOAD)
    if not xloadflags & XLF_KERNEL_64:
        raise BzImageError("NOT_64BIT", f"xloadflags {xloadflags:#x}",
                           'B field xloadflags bit 0 XLF_KERNEL_64: "If 1, this kernel has the legacy 64-bit entry point at 0x200."')
    reloc, align = bool(data[0x234]), _u(data, 0x230, "I")
    if reloc and (align == 0 or align & (align - 1)):
        raise BzImageError("BAD_ALIGNMENT", f"kernel_alignment {align:#x} (relocatable)", C_ALIGN)
    pref, init_size = _u(data, 0x258, "Q"), _u(data, 0x260, "I")
    if init_size == 0:
        raise BzImageError("BAD_INIT_SIZE", "init_size is 0", C_RUNTIME)
    if not reloc and pref == 0:
        raise BzImageError("BAD_PREF_ADDRESS", "non-relocatable with pref_address 0", C_RUNTIME)
    sects_raw = data[0x1F1]
    sects = sects_raw or 4  # B field setup_sects
    syssize = _u(data, 0x1F4, "I")
    pm_off, pm_size = (sects + 1) * 512, syssize * 16
    if len(data) < pm_off + pm_size:
        raise BzImageError("TRUNCATED_IMAGE", f"{len(data)} bytes < setup {pm_off} + syssize*16 {pm_size}",
                           'B fields setup_sects, syssize ("size of the protected-mode code in units of 16-byte paragraphs")')
    pay_off, pay_len = _u(data, 0x248, "I"), _u(data, 0x24C, "I")
    pay_file = None
    if pay_off:
        if pay_off + pay_len > pm_size:
            raise BzImageError("BAD_PAYLOAD", f"payload {pay_off:#x}+{pay_len:#x} outside protected-mode code {pm_size:#x}",
                               'B fields payload_offset/payload_length')
        pay_file = pm_off + pay_off
    return BzImage(version, sects_raw, sects, syssize, loadflags, True, xloadflags, True,
                   bool(xloadflags & XLF_ABOVE_4G), align, reloc, data[0x235], pref, init_size,
                   _u(data, 0x238, "I"), _u(data, 0x22C, "I"), _u(data, 0x264, "I"), _u(data, 0x214, "I"),
                   pay_off, pay_len, header_end, pm_off, pm_size, pay_file, bytes(data[0x1F1:header_end]))


# ---- loaded-state checker --------------------------------------------------------------------
@dataclass
class Layout:
    """What the loader says it did. memory_map: expected e820 as (addr, size, type) tuples."""
    image: bytes
    kernel_addr: int
    zero_page: int
    cmdline_addr: int
    memory_map: list
    initrd: tuple | None = None  # (addr, size)
    rsdp_addr: int | None = None
    setup_data: int = 0  # head of the setup_data list the loader says it passed; 0 = no list


# Header fields typed "read" in boot.rst: the copy in boot_params must equal the image's.
READ_FIELDS = [("setup_sects", 0x1F1, 1), ("syssize", 0x1F4, 4), ("boot_flag", 0x1FE, 2), ("jump", 0x200, 2),
               ("header", 0x202, 4), ("version", 0x206, 2), ("initrd_addr_max", 0x22C, 4),
               ("relocatable_kernel", 0x234, 1), ("min_alignment", 0x235, 1), ("xloadflags", 0x236, 2),
               ("cmdline_size", 0x238, 4), ("payload_offset", 0x248, 4), ("payload_length", 0x24C, 4),
               ("pref_address", 0x258, 8), ("init_size", 0x260, 4), ("handover_offset", 0x264, 4)]


class _Unreadable(Exception):
    pass


def _read(mem, addr, n):
    if hasattr(mem, "read"):
        try:
            b = mem.read(addr, n)
        except Exception as e:  # noqa: BLE001 - any backend failure is "unreadable"
            raise _Unreadable(str(e)) from e
    else:
        if addr < 0 or addr + n > len(mem):
            raise _Unreadable(f"{addr:#x}+{n:#x} outside flat memory")
        b = bytes(mem[addr:addr + n])
    if len(b) != n:
        raise _Unreadable(f"short read at {addr:#x}")
    return b


def _normalize(m):
    out = []
    for a, s, t in sorted(m):
        if out and out[-1][2] == t and out[-1][0] + out[-1][1] == a:
            out[-1] = (out[-1][0], out[-1][1] + s, t)
        else:
            out.append((a, s, t))
    return out


def _covered(norm, start, end, types):
    """True if [start,end) lies inside one merged e820 extent whose type is in `types`."""
    return any(a <= start and end <= a + s and t in types for a, s, t in norm)


def _walk(mem, cr3, va):
    """4-level walk; returns physical address or None (not present). 2M/1G pages accepted."""
    table = cr3 & 0x000FFFFFFFFFF000
    for shift in (39, 30, 21, 12):
        e = struct.unpack("<Q", _read(mem, table + ((va >> shift) & 0x1FF) * 8, 8))[0]
        if not e & 1:
            return None
        if shift == 12 or (shift in (30, 21) and e & 0x80):
            size = 1 << shift
            return (e & 0x000FFFFFFFFFF000 & ~(size - 1)) | (va & (size - 1))
        table = e & 0x000FFFFFFFFFF000
    return None


def _desc(mem, base, sel):
    q = struct.unpack("<Q", _read(mem, base + (sel & ~7), 8))[0]
    return dict(limit=(q & 0xFFFF) | ((q >> 48) & 0xF) << 16, base=((q >> 16) & 0xFFFFFF) | (q >> 56) << 24,
                type=(q >> 40) & 0xF, s=(q >> 44) & 1, p=(q >> 47) & 1, l=(q >> 53) & 1, d=(q >> 54) & 1,
                g=(q >> 55) & 1)


def _flat(d):
    return d["base"] == 0 and d["limit"] == 0xFFFFF and d["g"] == 1


def check_loaded(memory, rip, rsi, regs, layout: Layout) -> list:
    """Judge a loader's result against the 64-bit boot protocol; return a list of Violation."""
    v: list = []

    def bad(rule, detail, cite):
        v.append(Violation(rule, detail, cite))

    img = parse_bzimage(layout.image)
    k, zp = layout.kernel_addr, layout.zero_page

    # -- zero page at RSI, header copied -------------------------------------------------------
    if rsi != zp:
        bad("ZP-RSI", f"rsi {rsi:#x} != zero page {zp:#x}", C_RSI)
    try:
        z = _read(memory, zp, PAGE)
    except _Unreadable as e:
        return v + [Violation("ZP-UNREADABLE", f"zero page at {zp:#x}: {e}", C_ZERO)]
    if z[0x1EF] != 0:
        bad("ZP-SENTINEL", f"sentinel {z[0x1EF]:#x} != 0 (header copied from 0x1ef or zero page not zeroed)", C_SENT)
    for name, off, n in READ_FIELDS:
        if z[off:off + n] != layout.image[off:off + n]:
            bad("ZP-HDR", f"{name} at {off:#x} differs from the image", C_ZERO)
    if not z[0x211] & LOADED_HIGH:
        bad("ZP-HDR", "loadflags LOADED_HIGH cleared", C_ZERO)
    tol = z[0x210]
    if tol == 0:
        bad("ZP-LOADER", "type_of_loader is 0", C_LOADER)

    # -- kernel placement ----------------------------------------------------------------------
    eff_align = _u(z, 0x230, "I")
    if img.relocatable_kernel:
        if eff_align == 0 or eff_align & (eff_align - 1) or eff_align > img.kernel_alignment or \
                (img.min_alignment and eff_align < 1 << img.min_alignment):
            bad("ZP-ALIGN", f"zero-page kernel_alignment {eff_align:#x} not a power of two in "
                f"[1<<min_alignment={1 << img.min_alignment if img.min_alignment else 1:#x}, "
                f"image {img.kernel_alignment:#x}]", C_ALIGN)
            eff_align = img.kernel_alignment
        if k % eff_align:
            bad("K-ALIGN", f"kernel {k:#x} not aligned to {eff_align:#x}", C_ALIGN)
        if k < img.pref_address:
            bad("K-PREF", f"kernel {k:#x} below pref_address {img.pref_address:#x}", C_PREF + " / " + C_RUNTIME)
    elif k != img.pref_address:
        bad("K-PREF", f"non-relocatable kernel at {k:#x}, must run at pref_address {img.pref_address:#x}", C_RUNTIME)
    if _u(z, 0x214, "I") != k & 0xFFFFFFFF:
        bad("ZP-CODE32", f"code32_start {_u(z, 0x214, 'I'):#x} != kernel {k:#x}", C_C32)
    if rip != k + 0x200:
        bad("ENTRY-RIP", f"rip {rip:#x} != kernel {k:#x} + 0x200", C_ENTRY)
    try:
        want = layout.image[img.pm_offset:img.pm_offset + img.pm_size]
        if _read(memory, k, len(want)) != want:
            bad("K-COPY", "protected-mode code in memory differs from the image", C_LOAD)
    except _Unreadable as e:
        bad("K-COPY", f"kernel unreadable: {e}", C_LOAD)

    # -- command line --------------------------------------------------------------------------
    cmd = _u(z, 0x228, "I") | _u(z, 0x0C8, "I") << 32
    if _u(z, 0x228, "I") == 0 and _u(z, 0x0C8, "I") == 0:
        bad("CMD-PTR", "cmd_line_ptr is zero", C_CMDPTR)
    elif cmd != layout.cmdline_addr:
        bad("CMD-PTR", f"cmd_line_ptr {cmd:#x} != layout {layout.cmdline_addr:#x}", C_CMDPTR)
    cmd_len = img.cmdline_size
    try:
        s = _read(memory, layout.cmdline_addr, img.cmdline_size + 1)
        if 0 in s:
            cmd_len = s.index(0)
        else:
            bad("CMD-TERM", f"no NUL within cmdline_size {img.cmdline_size} + 1 bytes", C_CMDSZ)
    except _Unreadable as e:
        bad("CMD-TERM", f"command line unreadable: {e}", C_CMDSZ)

    # -- initrd --------------------------------------------------------------------------------
    rd = _u(z, 0x218, "I") | _u(z, 0x0C0, "I") << 32
    rds = _u(z, 0x21C, "I") | _u(z, 0x0C4, "I") << 32
    if layout.initrd is None:
        if rd or rds:
            bad("INITRD-FIELDS", f"no initrd but ramdisk_image/size = {rd:#x}/{rds:#x}", C_INITRD0)
    else:
        ia, isz = layout.initrd
        if (rd, rds) != (ia, isz):
            bad("INITRD-FIELDS", f"ramdisk_image/size {rd:#x}/{rds:#x} != layout {ia:#x}/{isz:#x}", C_INITRD)
        if isz and ia + isz - 1 > img.initrd_addr_max:
            if ia >= 1 << 32 and img.above_4g:
                pass  # allowed by XLF_CAN_BE_LOADED_ABOVE_4G
            else:
                bad("INITRD-MAX", f"initrd last byte {ia + isz - 1:#x} > initrd_addr_max {img.initrd_addr_max:#x}"
                    + ("" if img.above_4g else " (XLF_CAN_BE_LOADED_ABOVE_4G clear)"), C_IMAX + " / " + C_4G)

    # -- above-4G users need the bit ------------------------------------------------------------
    objs = {"kernel": (k, k + img.init_size), "zero page": (zp, zp + PAGE),
            "cmdline": (layout.cmdline_addr, layout.cmdline_addr + cmd_len + 1)}
    if layout.initrd and layout.initrd[1]:
        objs["initrd"] = (layout.initrd[0], layout.initrd[0] + layout.initrd[1])
    if not img.above_4g:
        for n, (a, e) in objs.items():
            if e > 1 << 32:
                bad("ABOVE-4G", f"{n} {a:#x}..{e:#x} above 4G but XLF_CAN_BE_LOADED_ABOVE_4G is clear", C_4G)
        if _u(z, 0x0C8, "I"):
            bad("ABOVE-4G", "ext_cmd_line_ptr nonzero without XLF_CAN_BE_LOADED_ABOVE_4G", C_EXTCMD)

    # -- e820 -----------------------------------------------------------------------------------
    n = z[0x1E8]
    tab = []
    if n == 0 or n > E820_MAX:
        bad("E820-COUNT", f"e820_entries {n} not in 1..{E820_MAX}", C_E820N)
        n = min(n, E820_MAX)
    for i in range(n):
        tab.append(struct.unpack_from("<QQI", z, 0x2D0 + 20 * i))
    prev_end = -1
    for i, (a, s, t) in enumerate(tab):
        if s == 0 or a + s > 1 << 64:
            bad("E820-ENTRY", f"entry {i} addr {a:#x} size {s:#x}", C_E820)
        if t not in E820_TYPES:
            bad("E820-TYPE", f"entry {i} type {t}", C_E820T)
        if i and a < tab[i - 1][0]:
            bad("E820-ORDER", f"entry {i} addr {a:#x} below entry {i - 1}", C_E820)
        if a < prev_end:
            bad("E820-OVERLAP", f"entry {i} {a:#x} overlaps previous end {prev_end:#x}", C_E820)
        prev_end = max(prev_end, a + s)
    norm = _normalize(tab)
    if norm != _normalize(layout.memory_map):
        bad("E820-MAP", f"table {norm} != layout {_normalize(layout.memory_map)}", C_E820)
    for name, (a, e) in objs.items():
        if not _covered(norm, a, e, {E820_RAM}):  # ponytail: boot.rst is silent on e820 types; usable RAM required
            bad("PLACE-RAM", f"{name} {a:#x}..{e:#x} not inside one e820 RAM extent", C_E820)
    names = list(objs)
    for i, x in enumerate(names):
        for y in names[i + 1:]:
            if objs[x][0] < objs[y][1] and objs[y][0] < objs[x][1]:
                bad("PLACE-OVERLAP", f"{x} {objs[x][0]:#x}..{objs[x][1]:#x} overlaps {y} {objs[y][0]:#x}..{objs[y][1]:#x}",
                    C_RUNTIME + " (init_size window is the kernel's)")

    # -- ACPI RSDP -----------------------------------------------------------------------------
    rsdp = _u(z, 0x70, "Q")
    if layout.rsdp_addr is not None:
        if rsdp != layout.rsdp_addr:
            bad("RSDP", f"acpi_rsdp_addr {rsdp:#x} != layout {layout.rsdp_addr:#x}", C_RSDP)
    if rsdp:
        try:
            r = _read(memory, rsdp, 20)
            if r[:8] != b"RSD PTR " or sum(r) & 0xFF:
                bad("RSDP", "bad signature or checksum at acpi_rsdp_addr", C_RSDPSIG)
        except _Unreadable as e:
            bad("RSDP", f"unreadable: {e}", C_RSDP)
        if not _covered(norm, rsdp, rsdp + 20, {E820_ACPI, E820_NVS, E820_RESERVED}):
            bad("RSDP", f"RSDP {rsdp:#x} not in an ACPI/NVS/reserved e820 extent", C_RSDPSIG)

    # -- setup_data list (loader-written; never passed through from the image) -----------------
    sd = _u(z, 0x250, "Q")
    if sd != layout.setup_data:
        bad("ZP-SETUP-DATA", f"setup_data {sd:#x} != layout {layout.setup_data:#x} (field is the loader's to write)", C_SETUP)
    seen, node = set(), sd
    while node:
        if node in seen or len(seen) >= SETUP_DATA_MAX_NODES:
            bad("ZP-SETUP-DATA", f"list does not terminate within {SETUP_DATA_MAX_NODES} nodes (node {node:#x})", C_SETUP)
            break
        seen.add(node)
        try:
            nxt, _typ, ln = struct.unpack("<QII", _read(memory, node, 16))
            _read(memory, node + 16, ln)
        except _Unreadable as e:
            bad("ZP-SETUP-DATA", f"node {node:#x} unreadable: {e}", C_SETUP)
            break
        node = nxt

    # -- CPU state -----------------------------------------------------------------------------
    cr0, cr4, efer = regs["cr0"], regs["cr4"], regs["efer"]
    if not (cr0 & 1 and cr0 >> 31 & 1 and cr4 >> 5 & 1 and efer >> 8 & 1 and efer >> 10 & 1):
        bad("CPU-MODE", f"need CR0.PE+PG, CR4.PAE, EFER.LME+LMA: cr0={cr0:#x} cr4={cr4:#x} efer={efer:#x}", C_MODE)
    if regs["rflags"] >> 9 & 1:
        bad("CPU-IF", "RFLAGS.IF is set", C_IF)
    if regs["cs"] != BOOT_CS or any(regs[r] != BOOT_DS for r in ("ds", "es", "ss")):
        bad("CPU-SEL", f"cs={regs['cs']:#x} ds={regs['ds']:#x} es={regs['es']:#x} ss={regs['ss']:#x}", C_SEL)
    try:
        if regs["gdtr_limit"] < BOOT_DS + 7:
            bad("CPU-GDT", f"GDT limit {regs['gdtr_limit']:#x} excludes selector 0x18", C_GDT)
        else:
            cs, ds = _desc(memory, regs["gdtr_base"], BOOT_CS), _desc(memory, regs["gdtr_base"], BOOT_DS)
            if not (cs["p"] and cs["s"] and cs["type"] & 8 and cs["type"] & 2):
                bad("CPU-GDT-CS", f"__BOOT_CS not present, code, readable: {cs}", C_CS)
            if not _flat(cs):
                bad("CPU-GDT-FLAT", f"__BOOT_CS not 4G flat: {cs}", C_GDT)
            if not (cs["l"] and not cs["d"]):
                bad("CPU-GDT-CS", f"__BOOT_CS is not a 64-bit code segment (L=1, D=0): {cs}", C_MODE)
            if not (ds["p"] and ds["s"] and not ds["type"] & 8 and ds["type"] & 2):
                bad("CPU-GDT-DS", f"__BOOT_DS not present, data, writable: {ds}", C_DS)
            if not _flat(ds):
                bad("CPU-GDT-FLAT", f"__BOOT_DS not 4G flat: {ds}", C_GDT)
    except _Unreadable as e:
        bad("CPU-GDT", f"GDT unreadable: {e}", C_GDT)

    # -- identity mapping --------------------------------------------------------------------------
    if cr0 >> 31 & 1 and cr4 >> 5 & 1:
        if cr4 >> 12 & 1:
            bad("CPU-MODE", "CR4.LA57 set: this checker walks 4-level tables only", C_IDENT)
        else:
            for name, (a, e) in objs.items():
                try:
                    for p in range(a & ~(PAGE - 1), e, PAGE):
                        if _walk(memory, regs["cr3"], p) != p:
                            bad("IDENT-MAP", f"{name}: page {p:#x} not identity-mapped", C_IDENT)
                            break
                except _Unreadable as e2:
                    bad("IDENT-MAP", f"{name}: page tables unreadable: {e2}", C_IDENT)
    return v

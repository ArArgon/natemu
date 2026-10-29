"""Tests for bootproto.py. Run: python3 tools/models/test_bootproto.py  (or python3 -m unittest)."""
import hashlib
import pathlib
import struct
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))   # tools: runnable by path from the repo root
from models import bootproto as bp

DEBIAN = pathlib.Path("/Users/liaozping/orca/natemu-raw/kernel-census-20261028-01/dl/linux")
DEBIAN_SHA = "2b2358b37674d2505350528875bb17afae2a36522a9e8a9417eaca65a7da0e08"   # experiments/track03/kernel-census/README.md
MIB = 1 << 20


class Mem:
    """Sparse guest memory: 4K pages, unwritten pages read as zero."""

    def __init__(self):
        self.p = {}

    def write(self, a, b):
        for i, x in enumerate(b):
            pg = self.p.setdefault((a + i) >> 12, bytearray(4096))
            pg[(a + i) & 4095] = x

    def write_bulk(self, a, b):
        i = 0
        while i < len(b):
            pg = self.p.setdefault((a + i) >> 12, bytearray(4096))
            off = (a + i) & 4095
            n = min(4096 - off, len(b) - i)
            pg[off:off + n] = b[i:i + n]
            i += n

    def read(self, a, n):
        out = bytearray()
        while len(out) < n:
            off = (a + len(out)) & 4095
            k = min(4096 - off, n - len(out))
            pg = self.p.get((a + len(out)) >> 12)
            out += pg[off:off + k] if pg else bytes(k)
        return bytes(out)


def make_image(*, setup_sects=4, pm_paras=0x1000, version=0x020F, xlf=0x3, loadflags=1, align=0x400000, reloc=1,
               minal=21, pref=0x1000000, init_size=0x400000, cmdsz=2047, imax=0x2FFFFFF, jump=0x6A,
               magic=b"HdrS", boot_flag=0xAA55, payload=(0x100, 0x800)):
    sects = setup_sects or 4
    d = bytearray((sects + 1) * 512 + pm_paras * 16)
    for i in range((sects + 1) * 512, len(d)):
        d[i] = (i * 7 + 1) & 0xFF
    d[0x1EF] = 0xFF
    d[0x1F1] = setup_sects
    struct.pack_into("<I", d, 0x1F4, pm_paras)
    struct.pack_into("<H", d, 0x1FE, boot_flag)
    d[0x200], d[0x201] = 0xEB, jump
    d[0x202:0x206] = magic
    struct.pack_into("<H", d, 0x206, version)
    d[0x211] = loadflags
    struct.pack_into("<I", d, 0x214, 0x100000)
    struct.pack_into("<I", d, 0x22C, imax)
    struct.pack_into("<I", d, 0x230, align)
    d[0x234], d[0x235] = reloc, minal
    struct.pack_into("<H", d, 0x236, xlf)
    struct.pack_into("<I", d, 0x238, cmdsz)
    struct.pack_into("<II", d, 0x248, *payload)
    struct.pack_into("<Q", d, 0x258, pref)
    struct.pack_into("<I", d, 0x260, init_size)
    struct.pack_into("<I", d, 0x264, 0x1234)
    return bytes(d)


class Env:
    """A correct loaded state; tests mutate one thing and call check()."""

    RAM_END = 128 * MIB
    ACPI = 120 * MIB

    def __init__(self, image=None, kernel=0x1000000, initrd=(0x2000000, MIB), cmd=b"console=ttyS0\0",
                 zp=0x10000, cmdline=0x11000, above4g_initrd=False, **imgkw):
        self.image = image or make_image(**imgkw)
        self.img = bp.parse_bzimage(self.image)
        self.mem = m = Mem()
        self.kernel, self.zp_addr, self.cmd_addr = kernel, zp, cmdline
        self.map = [(0, 0x9F000, 1), (0x9F000, 0x1000, 2), (MIB, self.ACPI - MIB, 1),
                    (self.ACPI, 0x10000, 3), (self.ACPI + 0x10000, self.RAM_END - self.ACPI - 0x10000, 1)]
        if above4g_initrd:
            initrd = (1 << 32, MIB)
            self.map.append((1 << 32, 2 * MIB, 1))
        self.layout = bp.Layout(self.image, kernel, zp, cmdline, self.map, initrd, self.ACPI)
        i = self.img
        m.write_bulk(kernel, self.image[i.pm_offset:i.pm_offset + i.pm_size])
        m.write(cmdline, cmd)
        z = bytearray(4096)
        z[0x1F1:i.header_end] = i.header
        z[0x210] = 0xFF
        struct.pack_into("<I", z, 0x214, kernel)
        struct.pack_into("<I", z, 0x228, cmdline)
        if initrd:
            struct.pack_into("<I", z, 0x218, initrd[0] & 0xFFFFFFFF)
            struct.pack_into("<I", z, 0x0C0, initrd[0] >> 32)
            struct.pack_into("<I", z, 0x21C, initrd[1])
        z[0x1E8] = len(self.map)
        for n, t in enumerate(self.map):
            struct.pack_into("<QQI", z, 0x2D0 + 20 * n, *t)
        struct.pack_into("<Q", z, 0x70, self.ACPI)
        m.write(zp, z)
        r = bytearray(b"RSD PTR " + bytes(12))
        r[8] = -sum(r) & 0xFF
        m.write(self.ACPI, r)
        # GDT at 0x12000: null, null, __BOOT_CS (L), __BOOT_DS
        m.write(0x12000, struct.pack("<4Q", 0, 0, 0x00AF9A000000FFFF, 0x00CF92000000FFFF))
        # identity map of the first 1 GiB with 2 MiB pages
        m.write(0x20000, struct.pack("<Q", 0x21000 | 3))
        m.write(0x21000, struct.pack("<Q", 0x22000 | 3))
        m.write(0x22000, b"".join(struct.pack("<Q", (n << 21) | 0x83) for n in range(512)))
        self.rip, self.rsi = kernel + 0x200, zp
        self.regs = dict(cr0=0x80000001, cr3=0x20000, cr4=0x20, efer=0x500, rflags=0x2, cs=0x10, ds=0x18, es=0x18,
                         ss=0x18, gdtr_base=0x12000, gdtr_limit=0x1F)

    def zp(self, off, data):
        self.mem.write(self.zp_addr + off, data)

    def set_e820(self, entries):
        self.zp(0x1E8, bytes([len(entries)]))
        for n, t in enumerate(entries):
            self.zp(0x2D0 + 20 * n, struct.pack("<QQI", *t))

    def check(self):
        return bp.check_loaded(self.mem, self.rip, self.rsi, self.regs, self.layout)

    def rules(self):
        return {x.rule for x in self.check()}


class ParseTests(unittest.TestCase):
    def err(self, data):
        with self.assertRaises(bp.BzImageError) as c:
            bp.parse_bzimage(data)
        return c.exception.code

    @unittest.skipUnless(DEBIAN.exists(), "Debian installer kernel not present")
    def test_debian_header(self):
        d = DEBIAN.read_bytes()
        self.assertEqual(hashlib.sha256(d).hexdigest(), DEBIAN_SHA)
        i = bp.parse_bzimage(d)
        self.assertEqual(i.version, 0x020F)
        self.assertEqual((i.setup_sects, i.syssize, i.loadflags), (39, 0xB8F20, 1))
        self.assertTrue(i.loaded_high)
        self.assertEqual(i.xloadflags, 0x7F)
        self.assertTrue(i.kernel_64 and i.above_4g)
        self.assertEqual((i.kernel_alignment, i.relocatable_kernel, i.min_alignment), (0x200000, True, 21))
        self.assertEqual((i.pref_address, i.init_size), (0x1000000, 0x37EF000))
        self.assertEqual((i.cmdline_size, i.initrd_addr_max, i.handover_offset), (2047, 0x7FFFFFFF, 0xB7BBE0))
        self.assertEqual((i.payload_offset, i.payload_length, i.header_end), (0x2CC, 0xB575E2, 0x26C))
        self.assertEqual((i.pm_offset, i.pm_size), (40 * 512, 0xB8F200))
        self.assertEqual(i.payload_file_offset, 40 * 512 + 0x2CC)

    def test_synthetic_ok_and_setup_sects_zero(self):
        self.assertEqual(bp.parse_bzimage(make_image(setup_sects=0)).setup_sects, 4)
        self.assertEqual(bp.parse_bzimage(make_image()).setup_sects, 4)

    def test_bad_boot_flag(self):
        self.assertEqual(self.err(make_image(boot_flag=0x1234)), "BAD_BOOT_FLAG")

    def test_bad_magic(self):
        self.assertEqual(self.err(make_image(magic=b"HdrX")), "BAD_MAGIC")

    def test_version_below_2_12(self):
        self.assertEqual(self.err(make_image(version=0x020B)), "VERSION_TOO_OLD")
        self.assertEqual(bp.parse_bzimage(make_image(version=0x020C)).version, 0x020C)

    def test_missing_xlf_kernel_64(self):
        self.assertEqual(self.err(make_image(xlf=0x2)), "NOT_64BIT")

    def test_above_4g_bit_is_reported_not_required(self):
        self.assertFalse(bp.parse_bzimage(make_image(xlf=0x1)).above_4g)
        self.assertTrue(bp.parse_bzimage(make_image(xlf=0x3)).above_4g)

    def test_not_loaded_high(self):
        self.assertEqual(self.err(make_image(loadflags=0)), "NOT_LOADED_HIGH")

    def test_truncated_header(self):
        self.assertEqual(self.err(make_image()[:0x200]), "TRUNCATED_HEADER")
        self.assertEqual(self.err(b""), "TRUNCATED_HEADER")

    def test_truncated_image(self):
        self.assertEqual(self.err(make_image()[:-1]), "TRUNCATED_IMAGE")
        self.assertEqual(self.err(make_image()[:0x300]), "TRUNCATED_IMAGE")

    def test_bad_jump_and_header_end(self):
        self.assertEqual(self.err(make_image(jump=0x10)), "BAD_HEADER_END")
        d = bytearray(make_image())
        d[0x200] = 0
        self.assertEqual(self.err(bytes(d)), "BAD_JUMP")

    def test_bad_alignment_and_init_size(self):
        self.assertEqual(self.err(make_image(align=0x300000)), "BAD_ALIGNMENT")
        self.assertEqual(self.err(make_image(align=0)), "BAD_ALIGNMENT")
        self.assertEqual(self.err(make_image(init_size=0)), "BAD_INIT_SIZE")

    def test_non_relocatable_needs_pref(self):
        self.assertEqual(self.err(make_image(reloc=0, pref=0)), "BAD_PREF_ADDRESS")

    def test_payload_outside_image(self):
        self.assertEqual(self.err(make_image(payload=(0x100, 0x100000))), "BAD_PAYLOAD")


class CheckTests(unittest.TestCase):
    def only(self, e, rule):
        self.assertEqual(e.rules(), {rule}, [str(x) for x in e.check()])

    def test_good_state_passes(self):
        self.assertEqual(Env().check(), [])

    @unittest.skipUnless(DEBIAN.exists(), "Debian installer kernel not present")
    def test_good_state_with_debian_image(self):
        e = Env(image=DEBIAN.read_bytes(), initrd=(80 * MIB, MIB))   # window is 16..72 MiB
        self.assertEqual(e.check(), [])

    def test_loader_lowers_alignment_to_min_alignment(self):
        e = Env(kernel=0x1200000)   # 18 MiB: 2 MiB aligned, image kernel_alignment is 4 MiB
        self.only(e, "K-ALIGN")
        e.zp(0x230, struct.pack("<I", 0x200000))
        self.assertEqual(e.check(), [])

    def test_initrd_above_4g_allowed(self):
        e = Env(above4g_initrd=True)
        e.mem.write(0x21000 + 4 * 8, struct.pack("<Q", 0x23000 | 3))   # PDPT[4]: identity-map 4..5 GiB
        e.mem.write(0x23000, b"".join(struct.pack("<Q", (1 << 32) + (n << 21) | 0x83) for n in range(512)))
        self.assertEqual(e.check(), [])

    def test_no_initrd_passes(self):
        self.assertEqual(Env(initrd=None).check(), [])

    # --- zero page and header ---
    def test_rsi(self):
        e = Env(); e.rsi += 0x1000
        self.only(e, "ZP-RSI")

    def test_sentinel(self):
        e = Env(); e.zp(0x1EF, b"\xff")
        self.only(e, "ZP-SENTINEL")

    def test_header_not_copied(self):
        e = Env(); e.zp(0x236, b"\x01\x00")   # xloadflags differs
        self.only(e, "ZP-HDR")
        e = Env(); e.zp(0x260, struct.pack("<I", 1))   # init_size differs
        self.only(e, "ZP-HDR")

    def test_loadflags_high_cleared(self):
        e = Env(); e.zp(0x211, b"\x00")
        self.only(e, "ZP-HDR")

    def test_type_of_loader(self):
        e = Env(); e.zp(0x210, b"\x00")
        self.only(e, "ZP-LOADER")

    def test_zp_alignment_bad(self):
        e = Env(); e.zp(0x230, struct.pack("<I", 0x300000))
        self.assertIn("ZP-ALIGN", e.rules())
        e = Env(); e.zp(0x230, struct.pack("<I", 0x100000))   # below 1 << min_alignment
        self.assertIn("ZP-ALIGN", e.rules())

    def test_code32_start(self):
        e = Env(); e.zp(0x214, struct.pack("<I", 0x100000))
        self.only(e, "ZP-CODE32")

    # --- kernel ---
    def test_kernel_below_pref(self):
        self.only(Env(kernel=0x800000), "K-PREF")

    def test_nonrelocatable_must_run_at_pref(self):
        self.only(Env(reloc=0, kernel=0x1400000), "K-PREF")
        self.assertEqual(Env(reloc=0).check(), [])

    def test_kernel_misaligned(self):
        self.only(Env(kernel=0x1001000), "K-ALIGN")

    def test_entry_rip(self):
        e = Env(); e.rip = e.kernel
        self.only(e, "ENTRY-RIP")

    def test_kernel_copy_differs(self):
        e = Env(); e.mem.write(e.kernel + 100, b"\x00\x00")
        self.only(e, "K-COPY")

    # --- command line ---
    def test_cmdline_ptr_zero(self):
        e = Env(); e.zp(0x228, bytes(4))
        self.assertIn("CMD-PTR", e.rules())

    def test_cmdline_ptr_wrong(self):
        e = Env(); e.zp(0x228, struct.pack("<I", 0x13000))
        self.only(e, "CMD-PTR")

    def test_cmdline_not_terminated(self):
        e = Env(); e.mem.write(e.cmd_addr, b"x" * 2048)
        self.assertIn("CMD-TERM", e.rules())

    def test_cmdline_max_length_ok(self):
        e = Env(cmd=b"x" * 2047 + b"\0")
        self.assertEqual(e.check(), [])

    # --- initrd ---
    def test_initrd_fields_when_none(self):
        e = Env(initrd=None); e.zp(0x21C, struct.pack("<I", 5))
        self.only(e, "INITRD-FIELDS")

    def test_initrd_fields_mismatch(self):
        e = Env(); e.zp(0x21C, struct.pack("<I", 12345))
        self.only(e, "INITRD-FIELDS")

    def test_initrd_over_max(self):
        e = Env(initrd=(60 * MIB, MIB))   # image initrd_addr_max = 48 MiB - 1
        self.only(e, "INITRD-MAX")

    def test_initrd_last_byte_at_max_ok(self):
        e = Env(initrd=(0x3000000 - MIB, MIB))
        self.assertEqual(e.check(), [])

    def test_above_4g_without_bit(self):
        e = Env(above4g_initrd=True, xlf=0x1)
        self.assertLessEqual({"ABOVE-4G", "INITRD-MAX"}, e.rules())

    # --- e820 ---
    def test_e820_count_zero_and_too_many(self):
        e = Env(); e.zp(0x1E8, b"\x00")
        self.assertIn("E820-COUNT", e.rules())
        e = Env(); e.zp(0x1E8, b"\x81")
        self.assertIn("E820-COUNT", e.rules())

    def test_e820_unsorted(self):
        e = Env(); e.set_e820(list(reversed(e.map)))
        self.assertIn("E820-ORDER", e.rules())

    def test_e820_overlap(self):
        e = Env(); m = list(e.map); m[1] = (0x9E000, 0x2000, 2)
        e.set_e820(m)
        self.assertIn("E820-OVERLAP", e.rules())

    def test_e820_zero_size(self):
        e = Env(); e.set_e820(e.map + [(1 << 40, 0, 2)])
        self.assertIn("E820-ENTRY", e.rules())

    def test_e820_unknown_type(self):
        e = Env(); m = list(e.map); m[1] = (0x9F000, 0x1000, 0)
        e.set_e820(m)
        self.assertIn("E820-TYPE", e.rules())

    def test_e820_differs_from_layout(self):
        e = Env(); e.set_e820(e.map[:-1])
        self.assertIn("E820-MAP", e.rules())

    def test_e820_adjacent_ram_split_is_same_map(self):
        e = Env(); m = list(e.map); a, s, t = m[2]
        m[2:3] = [(a, s // 2, t), (a + s // 2, s - s // 2, t)]
        e.set_e820(m)
        self.assertEqual(e.check(), [])

    def test_object_outside_ram(self):
        e = Env(cmdline=0x9F000 + 16)   # inside the reserved range
        e.zp(0x228, struct.pack("<I", 0x9F010))
        self.assertIn("PLACE-RAM", e.rules())

    def test_zero_page_inside_kernel_window(self):
        e = Env(zp=0x1100000)
        self.assertIn("PLACE-OVERLAP", e.rules())

    def test_window_beyond_ram(self):
        e = Env(init_size=0x20000000)
        self.assertIn("PLACE-RAM", e.rules())

    # --- RSDP ---
    def test_rsdp_wrong_addr(self):
        e = Env(); e.zp(0x70, struct.pack("<Q", e.ACPI + 0x100))
        self.assertIn("RSDP", e.rules())

    def test_rsdp_bad_signature(self):
        e = Env(); e.mem.write(e.ACPI, b"XSD PTR ")
        self.only(e, "RSDP")

    def test_rsdp_in_plain_ram(self):
        e = Env(); e.zp(0x70, struct.pack("<Q", 0x300000)); e.layout.rsdp_addr = 0x300000
        e.mem.write(0x300000, e.mem.read(e.ACPI, 20))
        self.only(e, "RSDP")

    # --- CPU state ---
    def test_cpu_not_long_mode(self):
        for k, val in (("efer", 0x100), ("cr0", 0x1), ("cr4", 0), ("cr0", 0x80000000)):
            e = Env(); e.regs[k] = val
            self.assertIn("CPU-MODE", e.rules(), k)

    def test_interrupts_enabled(self):
        e = Env(); e.regs["rflags"] = 0x202
        self.only(e, "CPU-IF")

    def test_selectors(self):
        for k, val in (("cs", 0x08), ("ds", 0x10), ("es", 0), ("ss", 0x28)):
            e = Env(); e.regs[k] = val
            self.assertIn("CPU-SEL", e.rules(), k)

    def test_gdt_cs_not_readable_or_not_code(self):
        e = Env(); e.mem.write(0x12010, struct.pack("<Q", 0x00AF98000000FFFF))   # exec only, no read
        self.only(e, "CPU-GDT-CS")
        e = Env(); e.mem.write(0x12010, struct.pack("<Q", 0x00AF92000000FFFF))   # data segment
        self.only(e, "CPU-GDT-CS")

    def test_gdt_cs_not_64bit(self):
        e = Env(); e.mem.write(0x12010, struct.pack("<Q", 0x00CF9A000000FFFF))   # L=0, D=1
        self.only(e, "CPU-GDT-CS")

    def test_gdt_ds_not_writable(self):
        e = Env(); e.mem.write(0x12018, struct.pack("<Q", 0x00CF90000000FFFF))
        self.only(e, "CPU-GDT-DS")

    def test_gdt_not_flat(self):
        e = Env(); e.mem.write(0x12010, struct.pack("<Q", 0x00AF9A000000FFFE))   # limit 0xFFFFE pages
        self.only(e, "CPU-GDT-FLAT")
        e = Env(); e.mem.write(0x12018, struct.pack("<Q", 0x00CF92000010FFFF))   # base 0x10000
        self.only(e, "CPU-GDT-FLAT")

    def test_gdt_limit_too_small(self):
        e = Env(); e.regs["gdtr_limit"] = 0x17
        self.only(e, "CPU-GDT")

    # --- identity mapping ---
    def test_kernel_window_not_mapped(self):
        e = Env(); e.mem.write(0x22000 + 8 * 8, bytes(8))   # PD[8] covers 16..18 MiB
        self.only(e, "IDENT-MAP")

    def test_cmdline_not_identity_mapped(self):
        e = Env(); e.mem.write(0x22000, struct.pack("<Q", (5 << 21) | 0x83))   # 0..2 MiB maps to 10 MiB
        r = [x for x in e.check() if x.rule == "IDENT-MAP"]
        self.assertEqual({x.detail.split(":")[0] for x in r}, {"zero page", "cmdline"})

    def test_initrd_not_mapped(self):
        e = Env(); e.mem.write(0x22000 + 16 * 8, bytes(8))   # PD[16] covers 32..34 MiB
        r = [x for x in e.check() if x.rule == "IDENT-MAP"]
        self.assertEqual([x.detail.split(":")[0] for x in r], ["initrd"])

    def test_4k_pages_walk(self):
        e = Env()
        e.mem.write(0x22000, struct.pack("<Q", 0x23000 | 3))   # PD[0] -> PT with 4 KiB pages
        e.mem.write(0x23000, b"".join(struct.pack("<Q", (n << 12) | 3) for n in range(512)))
        self.assertEqual(e.check(), [])
        e.mem.write(0x23000 + 8 * 0x10, struct.pack("<Q", 0x99000 | 3))   # zero page 0x10000 remapped
        self.assertEqual({x.rule for x in e.check()}, {"IDENT-MAP"})


if __name__ == "__main__":
    unittest.main()

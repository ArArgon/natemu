"""sample_symbols on images built here: kallsyms tables in Linux 6.12's layout inside a synthetic kernel and bzImage, the KASLR
slide's markers, and a newc cpio holding ELF64 and ELF32 files whose executable segments hold a block's guest bytes. Each
lookup is also shown to miss where it should (outside a symbol, the wrong page offset, the wrong ELF class)."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import gzip, shutil, struct, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import sample_symbols as S

TEXT = 0xffff_ffff_8100_0000


def kallsyms_blob(symbols, relative_base=TEXT):
    """A kernel image's bytes holding kallsyms tables as 6.12's scripts/kallsyms.c lays them out: num_syms (8-aligned), the
    token-compressed names, the markers, the token table and index, the s32 offsets (relative: relative_base - 1 - offset;
    non-negative: absolute), the relative base. Token i is one character, so a name is its characters' codes."""
    toks = [bytes([i]) if 33 <= i < 127 else b'~%d' % i for i in range(256)]
    names = b''.join(bytes([len(n)]) + n.encode() for _, n in symbols)
    out = bytearray(b'\xaa' * 4096)
    out += struct.pack('<II', len(symbols), 0) + names
    out += bytes(-len(out) % 4)
    markers, at = [], 0
    for i, (_, n) in enumerate(symbols):
        if i % 256 == 0: markers.append(at)
        at += 1 + len(n)
    out += struct.pack(f'<{len(markers)}I', *markers)
    table = b''.join(t + b'\0' for t in toks)
    index, o = [], 0
    for t in toks: index.append(o); o += len(t) + 1
    out += table + bytes(-len(table) % 4)
    out += struct.pack('<256H', *index)
    offs = [a if a < 1 << 31 else relative_base - 1 - a for a, _ in symbols]
    out += struct.pack(f'<{len(offs)}i', *offs)
    out += bytes(-len(out) % 8) + struct.pack('<Q', relative_base) + b'\xbb' * 64
    return bytes(out)


SYMS = [(0x0, 'Afixed_percpu_data'), (TEXT, 'T_text'), (TEXT + 0x100, 'Tclear_page_erms'), (TEXT + 0x140, 'tcopy_page'),
        (TEXT + 0x200, 'Dsome_data'), (TEXT + 0x300, 'Tmemcpy'), (TEXT + 0x400, 'T_etext')]


class Kernel(unittest.TestCase):
    def test_kallsyms_names_text_symbols(self):
        k = S.Kallsyms(kallsyms_blob(SYMS))
        self.assertEqual(k.text, TEXT)
        self.assertEqual(k.lookup(TEXT + 0x105), ('clear_page_erms', 5))
        self.assertEqual(k.lookup(TEXT + 0x13f), ('clear_page_erms', 0x3f))
        self.assertEqual(k.lookup(TEXT + 0x140), ('copy_page', 0))
        self.assertEqual(k.lookup(TEXT + 0x250), ('copy_page', 0x110))   # data symbols are not text: copy_page runs to memcpy
        self.assertIsNone(k.lookup(TEXT - 1))
        self.assertIsNone(k.lookup(TEXT + 0x400 + 8))                    # past the last symbol

    def test_a_slid_address_is_looked_up_less_the_slide(self):
        k = S.Kallsyms(kallsyms_blob(SYMS))
        slide = 0x3200_0000
        self.assertEqual(k.lookup(TEXT + slide + 0x301 - slide), ('memcpy', 1))
        self.assertNotEqual(k.lookup(TEXT + slide + 0x301), ('memcpy', 1))

    def test_no_tables_is_an_error(self):
        with self.assertRaises(RuntimeError): S.Kallsyms(b'\x01' * 8192)

    def test_a_bzimage_payload_is_decompressed(self):
        vmlinux = b'\x7fELF' + kallsyms_blob(SYMS)
        payload = gzip.compress(vmlinux) + struct.pack('<I', len(vmlinux))
        setup = 4
        img = bytearray((setup + 1) * 512) + b'\0' * 0x100 + payload
        img[0x1f1] = setup
        struct.pack_into('<II', img, 0x248, 0x100, len(payload) - 4)
        self.assertEqual(S.bzimage_payload(bytes(img)), vmlinux)

    def test_the_table_the_framework_writes_reads_back(self):
        d = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d, True)
        k = S.Kallsyms(kallsyms_blob(SYMS))
        k.bzimage_sha256 = 'ab' * 32
        k.write_table(d / 'kallsyms.txt')
        t = S.kernel_symbols(d / 'kallsyms.txt')
        self.assertEqual((t.addrs, t.names, t.text, t.bzimage_sha256), (k.addrs, k.names, TEXT, 'ab' * 32))
        self.assertEqual(t.lookup(TEXT + 0x105), ('clear_page_erms', 5))

    def test_module_text_lies_outside_the_image_in_the_module_area(self):
        k = S.Kallsyms(kallsyms_blob(SYMS))
        self.assertTrue(k.module(0xffff_ffff_c012_3456))
        self.assertFalse(k.module(TEXT + 0x200))                         # the image's text
        self.assertFalse(k.module(0xffff_8880_0000_1000))                # the direct map: no module

    def test_a_zstd_kernel_without_a_decompressor_says_what_to_install(self):
        import unittest.mock as um
        with um.patch.dict(sys.modules, {'compression.zstd': None, 'compression': None}), \
                um.patch.object(S.shutil, 'which', lambda n: None), um.patch.object(S, 'ZSTD_PATHS', ()):
            with self.assertRaises(RuntimeError) as e: S.zstd_tool()
            self.assertIn('brew install zstd', str(e.exception))
        with um.patch.dict(sys.modules, {'compression.zstd': None, 'compression': None}), \
                um.patch.object(S.shutil, 'which', lambda n: '/x/zstd'):
            self.assertEqual(S.zstd_tool(), '/x/zstd')

    def test_ktext_lines(self):
        d = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d, True)
        for line, want in (('NATEMU-BENCH-KTEXT ffffffff81000000', (TEXT, None)), ('IO5 ktext ffffffffb3000000', (0xffff_ffff_b300_0000, None)),
                           ('NATEMU-BENCH-KTEXT hidden', None), ('IO5 begin use', None)):
            (d / 'm.tsv').write_text(f'1\tNATEMU-BENCH-PROFILE port=/dev/port\n2\t{line}\n')
            got = S.ktext(d / 'm.tsv')
            if want: self.assertEqual(got, want)
            else: self.assertEqual(got[0], None); self.assertTrue(got[1])


def elf64(text_vaddr, text, symbols):
    """An ELF64 with one executable PT_LOAD (file offset 0x1000 + the vaddr's page offset) and a .symtab of FUNC symbols."""
    po = text_vaddr % 4096
    body = bytearray(0x1000 + po) + text
    strtab = b'\0' + b''.join(n.encode() + b'\0' for _, _, n in symbols)
    syms = bytes(24)
    o = 1
    for v, sz, n in symbols:
        syms += struct.pack('<IBBHQQ', o, 0x12, 0, 1, v, sz); o += len(n) + 1
    symoff = len(body); body += syms
    stroff = len(body); body += strtab
    body += bytes(-len(body) % 8)
    shoff = len(body)
    sh = [bytes(64), struct.pack('<IIQQQQIIQQ', 0, 2, 0, 0, symoff, len(syms), 2, 0, 8, 24),
          struct.pack('<IIQQQQIIQQ', 0, 3, 0, 0, stroff, len(strtab), 0, 0, 1, 0)]
    body += b''.join(sh)
    hdr = bytearray(64)
    hdr[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<QQ', hdr, 0x20, 64, shoff)
    struct.pack_into('<HHHH', hdr, 0x36, 56, 1, 64, 3)
    ph = struct.pack('<IIQQQQQQ', 1, 5, 0x1000 + po - po, text_vaddr - po, 0, po + len(text), po + len(text), 4096)
    body[:64] = hdr
    body[64:64 + 56] = ph
    return bytes(body)


def elf32(text_vaddr, text):
    po = text_vaddr % 4096
    body = bytearray(0x1000 + po) + text
    hdr = bytearray(52)
    hdr[:7] = b'\x7fELF\x01\x01\x01'
    struct.pack_into('<II', hdr, 0x1c, 52, 0)
    struct.pack_into('<HHHH', hdr, 0x2a, 32, 1, 40, 0)
    body[:52] = hdr
    body[52:52 + 32] = struct.pack('<8I', 1, 0x1000, text_vaddr - po, 0, po + len(text), po + len(text), 5, 4096)
    return bytes(body)


def cpio(files):
    out = bytearray()
    for ino, (name, data, mode) in enumerate(files + [('TRAILER!!!', b'', 0)], 1):
        nb = name.encode() + b'\0'
        out += ('070701' + ''.join('%08X' % v for v in (ino, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(nb), 0))).encode() + nb
        out += bytes(-(110 + len(nb)) % 4) + data + bytes(-len(data) % 4)
    return bytes(out)


class Image(unittest.TestCase):
    BLOCK = bytes.fromhex('554889e54883ec10897dfc8b45fc')      # push rbp; mov rbp, rsp; sub rsp, 16; ...

    def setUp(self):
        d = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d, True)
        text = b'\xcc' * 0x80 + self.BLOCK + b'\xc3' * 16
        f64 = elf64(0x40_1000, text, [(0x40_1000, 0x80, 'before'), (0x40_1080, 0x20, 'work')])
        f32 = elf32(0x0804_9000, text)
        (d / 'i.cpio').write_bytes(cpio([('usr/bin', b'', 0o40755), ('usr/bin/prog', f64, 0o100755),
                                         ('usr/bin/prog32', f32, 0o100755), ('etc/motd', b'hello', 0o100644)]))
        self.image = S.Image(d / 'i.cpio')

    def test_the_image_lists_its_elf_files(self):
        self.assertEqual(sorted(e.name for e, _ in self.image.files), ['usr/bin/prog', 'usr/bin/prog32'])

    def test_a_block_is_found_at_its_page_offset_and_named_by_its_function(self):
        # A PIE-like mapping: the same page offset at another base.
        q = {'a': (0x080, self.BLOCK, True), 'wrong-offset': (0x081, self.BLOCK, True), 'b32': (0x080, self.BLOCK, False)}
        hits = self.image.match(q)
        (elf, va), = hits['a']
        self.assertEqual((elf.name, va), ('usr/bin/prog', 0x40_1080))
        self.assertEqual((elf.lookup(va + 4), elf.lookup(0x40_10a0)), ('work', None))
        self.assertNotIn('wrong-offset', hits)
        (e32, va32), = hits['b32']
        self.assertEqual((e32.name, va32), ('usr/bin/prog32', 0x0804_9080))
        self.assertIsNone(e32.lookup(va32))                      # no symbols

    def test_bytes_that_differ_after_the_prefix_are_not_a_match(self):
        other = self.BLOCK[:8] + b'\x00' * 6
        self.assertNotIn('x', self.image.match({'x': (0x080, other, True)}))


if __name__ == '__main__':
    unittest.main()

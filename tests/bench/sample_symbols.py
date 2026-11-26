"""Symbols for the EL0 sampler's report (docs/restructure/el0-sampler-plan.md chunk 1): kernel samples by the kernel's own
symbol table, user samples by finding their blocks' guest bytes in the bench image's binaries.

**Kernel.** Debian's linux-image package ships a stub System.map (its one line says the real one is in the -dbg package),
so the symbols come from the kernel itself: the kallsyms tables in the bzImage natemu boots (the `debian-kernel` resource,
`debian-kernel`; io5 boots the same file, run_io5.py's BZIMAGE_SHA). The framework reads them once, before the run, into its
`kernel-symbols` input (testkit/inputs.py kernel_symbols, `{kernel_symbols}`: `ADDRESS NAME` lines), which the report
reads. The payload (zstd for Debian's 6.12) is decompressed with Python's `compression.zstd` (3.14 and later) or Homebrew's
`zstd` (the README's test prerequisites); without either the input fails to build, with ZSTD_NEEDED, before any case
runs. The tables are found by their shape: the 256 tokens
and their u16 index, the u32 markers before them, `kallsyms_num_syms` (8-byte aligned) before the names, the s32 offsets
after the index and the relative base after those (6.12's layout; a negative offset is relative_base - 1 - offset, a
non-negative one an absolute per-CPU symbol). The guest's KASLR slide is its `_text` (the bench init's NATEMU-BENCH-KTEXT
line, io5's `IO5 ktext`) less the table's; a hidden `_text` (kptr_restrict) is read as no slide (the bench boots use
nokaslr) and noted.

**User.** A block's object carries the guest bytes it translated. The image (the bench initramfs, a newc cpio:
`{bench_image}`) is read here; each ELF's executable PT_LOAD segments are searched for the bytes at the block's page offset
(code is mapped at a page-aligned base, so a block's page offset is its offset in the segment's file pages). A match names
the file and the block's address in it, and the file's own symbols (.symtab where it has one, else .dynsym: Debian's
binaries are stripped, so most of libc's and cc1's functions have no name) name the function when the address lies inside a
sized function symbol. A block found in several files at once is ambiguous and named by the first, with the count noted.
"""
import bisect
import hashlib
import collections
import mmap
import pathlib
import re
import shutil
import struct
import subprocess


# --- the kernel ------------------------------------------------------------------------------------------------------
MODULES = (0xffff_ffff_c000_0000, 0xffff_ffff_ff00_0000)
MATCH_BYTES = 8           # a user block's guest bytes the image search keys on
ZSTD_PATHS = ('/opt/homebrew/bin/zstd', '/usr/local/bin/zstd')     # Homebrew's, when PATH lacks it
ZSTD_NEEDED = "Debian's kernel is zstd-compressed: reading its symbols needs Homebrew's zstd (brew install zstd) or Python 3.14+"


def zstd_tool():
    """How a zstd payload is decompressed: None for Python's own compression.zstd (3.14 and later), else the path of the zstd
    program (on PATH, or Homebrew's). RuntimeError (ZSTD_NEEDED) when there is neither."""
    try:
        from compression import zstd  # noqa: F401
        return None
    except ImportError:
        pass
    exe = shutil.which('zstd') or next((p for p in ZSTD_PATHS if pathlib.Path(p).is_file()), None)
    if not exe: raise RuntimeError(ZSTD_NEEDED)
    return exe


def bzimage_payload(data: bytes) -> bytes:
    """The decompressed kernel (an ELF vmlinux) of a bzImage."""
    setup = data[0x1f1] or 4
    off, length = struct.unpack_from('<II', data, 0x248)
    p = data[(setup + 1) * 512 + off:][:length]
    if p[:4] == b'\x28\xb5\x2f\xfd':
        exe = zstd_tool()
        if exe is None:
            from compression import zstd
            return zstd.ZstdDecompressor().decompress(p)
        return subprocess.run([exe, '-dc'], input=p[:-4], capture_output=True, check=True).stdout
    if p[:2] == b'\x1f\x8b':
        import zlib
        return zlib.decompressobj(31).decompress(p)
    if p[:6] == b'\xfd7zXZ\0':
        import lzma
        return lzma.LZMADecompressor().decompress(p)
    raise RuntimeError(f'unknown kernel payload {p[:4].hex()}')


class Kallsyms:
    """The text symbols of a kernel image: `lookup(addr)` gives (name, offset) inside one, or None."""

    def __init__(self, v: bytes):
        tok_index = tok_table = None
        for m in re.finditer(rb'\x00\x00[\x02-\x20]\x00', v):
            q = m.start()
            if q % 4: continue
            idx = struct.unpack_from('<256H', v, q)
            if idx[255] > 8192 or any(idx[i + 1] <= idx[i] for i in range(255)): continue
            for pad in range(8):
                end = q - pad
                if v[end - 1] != 0: continue
                last = end - 1 - (v.rfind(b'\0', 0, end - 1) + 1)
                s = end - (idx[255] + last + 1)
                if s > 0 and all(v[s + idx[i + 1] - 1] == 0 for i in range(255)):
                    tok_index, tok_table = q, s; break
            if tok_index: break
        if tok_index is None: raise RuntimeError('no kallsyms token table')
        idx = struct.unpack_from('<256H', v, tok_index)
        toks = [v[tok_table + idx[i]:v.index(b'\0', tok_table + idx[i])].decode('latin1') for i in range(256)]
        offsets = tok_index + 512
        # The relative base follows the offsets; their count is kallsyms_num_syms, found before the names.
        found = None
        for base_at in range(offsets + offsets % 8, min(len(v) - 8, offsets + 4 * 400_000), 8):
            rb, = struct.unpack_from('<Q', v, base_at)
            if rb >> 32 != 0xffff_ffff or rb & 0xfff: continue
            for N in ((base_at - offsets) // 4, (base_at - offsets) // 4 - 1):
                if N < 1: continue
                nm = (N + 255) // 256
                for pad in (0, 4):
                    mk_at = tok_table - pad - 4 * nm
                    mk = struct.unpack_from(f'<{nm}I', v, mk_at)
                    if mk[0] != 0 or any(mk[i] >= mk[i + 1] for i in range(nm - 1)): continue
                    ns = v.rfind(struct.pack('<II', N, 0), max(0, mk_at - 64 * N - 16), mk_at)
                    if ns >= 0 and ns % 8 == 0: found = (rb, N, ns + 8); break
                if found: break
            if found: break
        if not found: raise RuntimeError('no kallsyms offsets')
        rb, N, at = found
        offs = struct.unpack_from(f'<{N}i', v, offsets)
        syms = []
        for i in range(N):
            ln = v[at]; at += 1
            if ln & 0x80: ln = (ln & 0x7f) | v[at] << 7; at += 1
            name = ''.join(toks[b] for b in v[at:at + ln]); at += ln
            o = offs[i]
            addr = o if o >= 0 else rb - 1 - o
            if name[:1] in ('T', 't', 'W', 'w'): syms.append((addr, name[1:]))
        syms.sort()
        self.addrs = [a for a, _ in syms]
        self.names = [n for _, n in syms]
        self.text = self.lookup_addr('_text')
        self.bzimage_sha256 = None

    def lookup_addr(self, name):
        try: return self.addrs[self.names.index(name)]
        except ValueError: return None

    def lookup(self, addr):
        i = bisect.bisect_right(self.addrs, addr) - 1
        if i < 0: return None
        j = i
        while j + 1 < len(self.addrs) and self.addrs[j + 1] == self.addrs[i]: j += 1
        nxt = self.addrs[j + 1] if j + 1 < len(self.addrs) else None
        if nxt is None or addr >= nxt or addr - self.addrs[i] > 1 << 20: return None
        return self.names[i], addr - self.addrs[i]


    def write_table(self, path):
        """The table as `ADDRESS NAME` lines (hex, sorted) after a `# bzimage-sha256 HEX` line: testkit's kernel-symbols
        input, which the report reads."""
        pathlib.Path(path).write_text(f'# bzimage-sha256 {self.bzimage_sha256}\n'
                                      + ''.join(f'{a:x} {n}\n' for a, n in zip(self.addrs, self.names)))

    def module(self, addr):
        """`addr` (less the slide) lies outside the image's text, in x86-64's module area (MODULES_VADDR, 0xffffffffc0000000
        with KASLR's 1 GiB image, to MODULES_END)."""
        etext = self.lookup_addr('_etext')
        return MODULES[0] <= addr < MODULES[1] and not (self.text is not None and etext is not None and self.text <= addr < etext)

    @classmethod
    def from_table(cls, path):
        k = cls.__new__(cls)
        lines = pathlib.Path(path).read_text().splitlines()
        k.bzimage_sha256 = next((x.split()[2] for x in lines[:1] if x.startswith('# bzimage-sha256 ')), None)
        rows = [line.split(' ', 1) for line in lines if line.strip() and not line.startswith('#')]
        k.addrs, k.names = [int(a, 16) for a, _ in rows], [n for _, n in rows]
        k.text = k.lookup_addr('_text')
        return k


def kernel_symbols(path):
    """The kernel's text symbols from a table (write_table's, the framework's {kernel_symbols}) or from a bzImage."""
    data = pathlib.Path(path).read_bytes()
    if data[0x202:0x206] == b'HdrS':                                            # the bzImage's setup header
        k = Kallsyms(bzimage_payload(data)); k.bzimage_sha256 = hashlib.sha256(data).hexdigest()
        return k
    return Kallsyms.from_table(path)


def sha256(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def ktext(markers):
    """The guest's `_text` from a markers file (NATEMU-BENCH-KTEXT X / IO5 ktext X): (address or None, note or None)."""
    for line in pathlib.Path(markers).read_text(errors='replace').splitlines():
        text = line.partition('\t')[2].strip()
        m = re.match(r'(?:NATEMU-BENCH-KTEXT|IO5 ktext)\s+(\S+)', text)
        if m:
            if m[1] == 'hidden': return None, '_text hidden (kptr_restrict): no KASLR slide assumed'
            return int(m[1], 16), None
    return None, 'no _text line in the markers: no KASLR slide assumed'


# --- the image ---------------------------------------------------------------------------------------------------------
def cpio_files(data):
    """{name: (offset, size)} of the regular files of a newc cpio."""
    at, out = 0, {}
    while at + 110 <= len(data):
        h = bytes(data[at:at + 110])
        if h[:6] not in (b'070701', b'070702'): break
        mode, size, nlen = int(h[14:22], 16), int(h[54:62], 16), int(h[94:102], 16)
        name = bytes(data[at + 110:at + 110 + nlen - 1]).decode(errors='replace')
        body = at + 110 + nlen; body += -body % 4
        if name == 'TRAILER!!!': break
        if mode & 0o170000 == 0o100000: out[name] = (body, size)
        at = body + size; at += -at % 4
    return out


class Elf:
    """An ELF file's executable segments [(vaddr, file bytes)], its GNU build ID and its function symbols (sized: .symtab,
    else .dynsym; Image replaces a file's .dynsym by the .symtab of its separate debug file when the image has one)."""

    def __init__(self, name, e):
        self.name = name
        self.segments, self.symbols = [], []
        self.symtab, self.debug, self.build_id = False, None, None
        self.c64 = c64 = e[4] == 2
        if c64:
            phoff, shoff = struct.unpack_from('<QQ', e, 0x20)
            phentsize, phnum, shentsize, shnum = struct.unpack_from('<HHHH', e, 0x36)
        else:
            phoff, shoff = struct.unpack_from('<II', e, 0x1c)
            phentsize, phnum, shentsize, shnum = struct.unpack_from('<HHHH', e, 0x2a)
        for i in range(phnum):
            if c64: typ, flags, off, vaddr, _, filesz, _, _ = struct.unpack_from('<IIQQQQQQ', e, phoff + i * phentsize)
            else: typ, off, vaddr, _, filesz, _, flags, _ = struct.unpack_from('<8I', e, phoff + i * phentsize)
            if typ == 1 and flags & 1 and off + filesz <= len(e): self.segments.append((vaddr, off, filesz))
            if typ == 4 and self.build_id is None and off + filesz <= len(e): self.build_id = note_build_id(e, off, filesz)
        secs = []
        for i in range(shnum if shoff and shoff + shnum * shentsize <= len(e) else 0):
            if c64:
                _, typ, _, _, off, size, link, _, _, entsize = struct.unpack_from('<IIQQQQIIQQ', e, shoff + i * shentsize)
            else:
                _, typ, _, _, off, size, link, _, _, entsize = struct.unpack_from('<10I', e, shoff + i * shentsize)
            secs.append((typ, off, size, link, entsize))
        for want in (2, 11):                                  # SHT_SYMTAB, then SHT_DYNSYM
            for typ, off, size, link, entsize in secs:
                if typ != want or not entsize or link >= len(secs): continue
                stro = secs[link][1]
                for k in range(size // entsize):
                    if c64:
                        nm, info, _, shndx, value, sz = struct.unpack_from('<IBBHQQ', e, off + k * entsize)
                    else:
                        nm, value, sz, info, _, shndx = struct.unpack_from('<IIIBBH', e, off + k * entsize)
                    if info & 0xf != 2 or not sz or not value: continue      # STT_FUNC, sized
                    end = e.find(b'\0', stro + nm)
                    self.symbols.append((value, sz, bytes(e[stro + nm:end]).decode(errors='replace')))
            if self.symbols: self.symtab = want == 2; break
        self.symbols.sort()
        self.starts = [s[0] for s in self.symbols]

    def lookup(self, vaddr):
        i = bisect.bisect_right(self.starts, vaddr) - 1
        for k in range(i, max(-1, i - 8), -1):                # a few back: sized symbols may nest or overlap
            v, sz, nm = self.symbols[k]
            if v <= vaddr < v + sz: return nm
        return None


def note_build_id(e, off, size):
    """The NT_GNU_BUILD_ID (type 3, owner "GNU") of a PT_NOTE segment, as hex, or None."""
    end = off + size
    while off + 12 <= end:
        namesz, descsz, typ = struct.unpack_from('<III', e, off)
        name_at = off + 12; desc_at = name_at + (namesz + 3) // 4 * 4
        if typ == 3 and bytes(e[name_at:name_at + namesz]) == b'GNU\0': return bytes(e[desc_at:desc_at + descsz]).hex()
        off = desc_at + (descsz + 3) // 4 * 4
    return None


DEBUG_DIR = 'usr/lib/debug/'


class Image:
    """The bench image's ELF files: `match(queries)` finds blocks' guest bytes in their executable segments. Files under
    usr/lib/debug (the dbgsym packages' separate debug files: symbols, no code) are not searched; a searched file without
    a .symtab takes the .symtab of the debug file its build ID names (usr/lib/debug/.build-id/XX/REST.debug)."""

    def __init__(self, cpio):
        f = open(cpio, 'rb')
        self.data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        f.close()
        self.files, debug = [], {}
        for name, (off, size) in sorted(cpio_files(self.data).items()):
            if self.data[off:off + 4] == b'\x7fELF':
                if name.startswith(DEBUG_DIR) or name.startswith('./' + DEBUG_DIR):
                    m = re.search(r'\.build-id/([0-9a-f]{2})/([0-9a-f]+)\.debug$', name)
                    if m: debug[m[1] + m[2]] = (off, size)
                    continue
                e = self.data[off:off + size]
                try: self.files.append((Elf(name, e), off))
                except struct.error: continue
        self.debug_files = 0
        for elf, _ in self.files:
            if elf.symtab or not elf.build_id or elf.build_id not in debug: continue
            off, size = debug[elf.build_id]
            try: d = Elf(elf.name + '.debug', self.data[off:off + size])
            except struct.error: continue
            if d.symtab and d.symbols:
                elf.symbols, elf.starts, elf.symtab, elf.debug = d.symbols, d.starts, True, elf.build_id
                self.debug_files += 1

    def match(self, queries, key=MATCH_BYTES):
        """{query id: [(Elf, vaddr), ...]} for queries {id: (page offset, guest bytes, 64-bit)}: every place an ELF file of
        that class holds the bytes in an executable segment at that page offset."""
        out = collections.defaultdict(list)
        d = self.data
        for c64 in (True, False):
            want = collections.defaultdict(lambda: collections.defaultdict(list))
            for qid, (po, b, q64) in queries.items():
                if len(b) >= key and q64 == c64: want[po][bytes(b[:key])].append(qid)
            if want: self._scan(want, queries, c64, key, out)
        return out

    def _scan(self, want, queries, c64, key, out):
        d = self.data
        for elf, base in self.files:
            if elf.c64 != c64: continue
            for vaddr, off, size in elf.segments:
                start, end = base + off, base + off + size
                vpage = vaddr - vaddr % 4096
                first = start - (vaddr % 4096)              # file position of the segment's first page's offset 0
                for po, prefixes in want.items():
                    p = first + po
                    if p < start: p += 4096
                    while p + key <= end:
                        hits = prefixes.get(d[p:p + key])
                        if hits:
                            va = vpage + (p - first)
                            for qid in hits:
                                b = queries[qid][1]
                                if p + len(b) <= end and d[p:p + len(b)] == b: out[qid].append((elf, va))
                        p += 4096
        return out


def basename(name):
    return name.rsplit('/', 1)[-1]


# The kernel's copy and clear routines (section 7's D4 row), and glibc's AVX2 and EVEX string functions by name.
KERNEL_COPY = {'copy_page', 'clear_page_erms', 'clear_page_rep', 'clear_page_orig', 'rep_movs_alternative',
               'rep_stos_alternative', 'memcpy', '__memcpy', 'memset', '__memset', 'memmove', '__memmove',
               'copy_user_generic', '__copy_user_nocache', 'memcpy_orig', 'memmove_orig', 'memset_orig'}
AVX2_NAME = re.compile(r'_(avx2|evex)(\b|_)')

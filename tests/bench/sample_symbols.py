"""Symbols for the EL0 sampler's report (docs/restructure/el0-sampler-plan.md chunk 1): kernel samples by the kernel's own
symbol table, user samples by finding their blocks' guest bytes in the bench image's binaries.

**Kernel.** Debian's linux-image package ships a stub System.map (its one line says the real one is in the -dbg package),
so the symbols come from the kernel itself: the kallsyms tables in the bzImage natemu boots (the `debian-kernel` resource,
`{kernel}`; io5 boots the same file, run_io5.py's BZIMAGE_SHA). Its payload (zstd for Debian's 6.12) is decompressed with
Python's `compression.zstd` (3.14 and later) or the `zstd` program, and the tables are found by their shape: the 256 tokens
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
import collections
import mmap
import pathlib
import re
import shutil
import struct
import subprocess


# --- the kernel ------------------------------------------------------------------------------------------------------
def bzimage_payload(data: bytes) -> bytes:
    """The decompressed kernel (an ELF vmlinux) of a bzImage."""
    setup = data[0x1f1] or 4
    off, length = struct.unpack_from('<II', data, 0x248)
    p = data[(setup + 1) * 512 + off:][:length]
    if p[:4] == b'\x28\xb5\x2f\xfd':
        try:
            from compression import zstd                  # Python 3.14+
            return zstd.ZstdDecompressor().decompress(p)
        except ImportError:
            exe = shutil.which('zstd')
            if not exe: raise RuntimeError('the kernel is zstd-compressed: needs Python 3.14 (compression.zstd) or zstd')
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


def kernel_symbols(bzimage):
    return Kallsyms(bzimage_payload(pathlib.Path(bzimage).read_bytes()))


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
    """An ELF file's executable segments [(vaddr, file bytes)] and its function symbols (sized: .symtab, else .dynsym)."""

    def __init__(self, name, e):
        self.name = name
        self.segments, self.symbols = [], []
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


class Image:
    """The bench image's ELF files: `match(queries)` finds blocks' guest bytes in their executable segments."""

    def __init__(self, cpio):
        f = open(cpio, 'rb')
        self.data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        f.close()
        self.files = []
        for name, (off, size) in sorted(cpio_files(self.data).items()):
            if self.data[off:off + 4] == b'\x7fELF':
                e = self.data[off:off + size]
                try: self.files.append((Elf(name, e), off))
                except struct.error: continue

    def match(self, queries, key=8):
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

#!/usr/bin/env python3
"""L1 checks: the Linux 64-bit boot-protocol loader (`crates/host/src/boot.rs`, driver `boot-linux`; boot-features plan L1).

    python3 tools/run_l1.py --out DIR [--only check,rejections] [--host HOST] [--kernel BZIMAGE] [--initrd FILE]

- `check`: the loader's output for the Debian 6.12.107 bzImage (with and without its installer initrd) and for synthetic
  minimal bzImages built here (protocol 2.12, setup_sects 0, relocatable with a 4 MiB alignment above pref_address,
  non-relocatable at pref_address), at several low-RAM sizes, judged by `tools/models/bootproto.py check_loaded`
  (independent author: the zero page and its header copy, placement, command line, initrd, e820, GDT, registers,
  identity map) from the driver's dump (`--load-only --dump`: every range the loader wrote) and its JSON. Each must give
  no violation; three corrupted dumps (sentinel, the kernel's PDE, the command line's NUL) must give theirs.
- `rejections`: every header rejection of `bootproto.parse_bzimage`, applied to the Debian header and to the synthetic
  image: the driver refuses with the model's code (`load refused: CODE`); plus the loader's own refusals reachable from
  the command line (low RAM below the map's RAM start, a kernel window or initrd that does not fit, a command line over
  cmdline_size, ACPI tables outside the ACPI NVS range).

Writes DIR/l1.json; exit status 1 on any failure. The VM smoke test is `tools/run_l1_smoke.py`."""
import argparse, json, pathlib, struct, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
from raw_root import RAW_ROOT  # noqa: E402
sys.path.insert(0, str(ROOT / 'tools'))
from models import bootproto as bp  # noqa: E402

RAW = RAW_ROOT / 'kernel-census-20261028-01/dl'
MIB = 1 << 20
CMDLINE = 'mitigations=off nokaslr'


def build():
    subprocess.run(['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release', '-p', 'natemu-host'], cwd=ROOT, check=True)
    return ROOT / 'target/release/natemu-host'


def synthetic(*, version=0x020C, setup_sects=0, reloc=1, align=0x400000, pref=0x1200000, init_size=0x300000, pm=0x2000):
    """A minimal bzImage from boot.rst's header table alone: a jump at 0x200 over the header to 0x26c, a `hlt` loop
    as the 64-bit entry (protected-mode code + 0x200), the rest of the protected-mode code a byte pattern."""
    sects = setup_sects or 4
    d = bytearray((sects + 1) * 512 + pm)
    for i in range((sects + 1) * 512, len(d)):
        d[i] = (i * 13 + 5) & 0xFF
    d[(sects + 1) * 512 + 0x200:(sects + 1) * 512 + 0x203] = b'\xf4\xeb\xfd'
    d[0x1F1] = setup_sects
    struct.pack_into('<I', d, 0x1F4, pm // 16)
    struct.pack_into('<H', d, 0x1FE, 0xAA55)
    d[0x200:0x202] = b'\xeb\x66'                                    # header end 0x268 (protocol 2.12's last field)
    d[0x202:0x206] = b'HdrS'
    struct.pack_into('<H', d, 0x206, version)
    d[0x211] = 0x01
    struct.pack_into('<I', d, 0x22C, 0x37FFFFFF)
    struct.pack_into('<I', d, 0x230, align)
    d[0x234], d[0x235] = reloc, 21
    struct.pack_into('<H', d, 0x236, 0x01)                          # XLF_KERNEL_64 only
    struct.pack_into('<I', d, 0x238, 255)
    struct.pack_into('<Q', d, 0x258, pref)
    struct.pack_into('<I', d, 0x260, init_size)
    return bytes(d)


def stale(image):
    d = bytearray(image)
    struct.pack_into('<II', d, 0x218, 0x2000000, 0x1000)        # ramdisk_image, ramdisk_size
    struct.pack_into('<I', d, 0x224, 0xFE00)                    # heap_end_ptr
    struct.pack_into('<I', d, 0x23C, 2)                         # hardware_subarch
    struct.pack_into('<Q', d, 0x250, 0x123456)                  # setup_data
    return bytes(d)


class Mem:
    """Guest RAM [0, size) from a dump (records u64 gpa, u64 length, bytes); zero elsewhere."""

    def __init__(self, dump, size):
        self.pages, self.size = {}, size
        o = 0
        while o < len(dump):
            a, n = struct.unpack_from('<QQ', dump, o)
            self.write(a, dump[o + 16:o + 16 + n])
            o += 16 + n

    def write(self, a, b):
        i = 0
        while i < len(b):
            p = self.pages.setdefault((a + i) >> 12, bytearray(4096))
            off = (a + i) & 4095
            k = min(4096 - off, len(b) - i)
            p[off:off + k] = b[i:i + k]
            i += k

    def read(self, a, n):
        if a < 0 or a + n > self.size:
            raise ValueError(f'{a:#x}+{n:#x} outside guest RAM')
        out = bytearray()
        while len(out) < n:
            off, k = (a + len(out)) & 4095, min(4096 - ((a + len(out)) & 4095), n - len(out))
            p = self.pages.get((a + len(out)) >> 12)
            out += p[off:off + k] if p else bytes(k)
        return bytes(out)


def load(host, work, name, image, *extra):
    """Runs the driver load-only; returns (returncode, JSON or None, stderr, dump path)."""
    work.mkdir(parents=True, exist_ok=True)
    img, dump = work / f'{name}.bzImage', work / f'{name}.dump'
    if isinstance(image, bytes):
        img.write_bytes(image)
    else:
        img = image
    r = subprocess.run([host, 'unused', 'boot-linux', 'unused', img, '--load-only', '--dump', dump, *map(str, extra)],
                       capture_output=True, text=True, timeout=300)
    if isinstance(image, bytes):
        img.unlink()
    return r.returncode, (json.loads(r.stdout) if r.returncode == 0 else None), r.stderr.strip(), dump


def judge(out, image, dump, rsdp=None, corrupt=None):
    n = lambda v: int(v, 0)  # noqa: E731
    e = out['entry']
    mem = Mem(dump.read_bytes(), n(out['low_end']))
    if corrupt:
        corrupt(mem, out)
    seg = [n(s) for s in e['seg']]  # ES CS SS DS FS GS
    regs = dict(cr0=n(e['cr0']), cr3=n(e['cr3']), cr4=n(e['cr4']), efer=n(e['efer']), rflags=n(e['rflags']),
                cs=seg[1], ds=seg[3], es=seg[0], ss=seg[2], gdtr_base=n(e['gdtr'][0]), gdtr_limit=n(e['gdtr'][1]))
    rd = out['initrd'] and (n(out['initrd'][0]), n(out['initrd'][1]))
    layout = bp.Layout(image, n(out['kernel']), n(out['zero_page']), n(out['cmdline']),
                       [(n(a), n(s), t) for a, s, t in out['memory_map']], rd or None, rsdp)
    return bp.check_loaded(mem, n(e['rip']), n(e['gpr'][6]), regs, layout)


def check(host, work, kernel, initrd):
    results = []
    deb = kernel.read_bytes()
    cases = [('debian', deb, ['--cmdline', CMDLINE]),
             ('debian-initrd', deb, ['--cmdline', CMDLINE, '--initrd', initrd]),
             ('debian-initrd-3g', deb, ['--cmdline', CMDLINE, '--initrd', initrd, '--ram', 3 << 30]),
             ('debian-128m', deb, ['--cmdline', CMDLINE, '--ram', 128 * MIB]),
             ('synthetic', synthetic(), ['--cmdline', 'x' * 255]),
             ('synthetic-64m-initrd', synthetic(), ['--ram', 64 * MIB, '--initrd', ROOT / 'tools/run_l1.py']),
             ('synthetic-fixed', synthetic(reloc=0, align=0, pref=0x2000000, setup_sects=7), ['--ram', 64 * MIB]),
             ('synthetic-2.15', synthetic(version=0x020F), []),
             # The image's values in fields boot.rst types "write" (ramdisk_image/size, setup_data, hardware_subarch,
             # heap_end_ptr) must not reach the kernel.
             ('synthetic-stale-write-fields', stale(synthetic()), ['--ram', 64 * MIB])]
    for name, image, extra in cases:
        code, out, err, dump = load(host, work, name, image, *extra)
        v = judge(out, image, dump) if out else [f'driver failed: {err}']
        if out and name.endswith('stale-write-fields'):  # beyond the checker's own rules: every stale field is zero
            z = Mem(dump.read_bytes(), int(out['low_end'], 0)).read(int(out['zero_page'], 0), 4096)
            left = [hex(o) for o, n in ((0x218, 4), (0x21C, 4), (0x224, 2), (0x23C, 4), (0x250, 8)) if any(z[o:o + n])]
            v = v + [f'WRITE-FIELDS: image values left at {left}'] if left else v
        if out and out['initrd']:  # the checker reads no initrd bytes: compare them here
            a, size = (int(x, 0) for x in out['initrd'])
            rd = pathlib.Path(extra[extra.index('--initrd') + 1]).read_bytes()
            if Mem(dump.read_bytes(), int(out['low_end'], 0)).read(a, size) != rd:
                v = v + ['INITRD-BYTES: initrd contents differ from the file']
        results.append(dict(case=name, kernel=out and out['kernel'], initrd=out and out['initrd'],
                            violations=[str(x) for x in v], ok=not v))
        if name == 'debian':
            controls = [('ZP-SENTINEL', lambda m, o: m.write(int(o['zero_page'], 0) + 0x1EF, b'\xff')),
                        ('IDENT-MAP', lambda m, o: m.write(0x102000 + 8 * (int(o['kernel'], 0) >> 21), bytes(8))),
                        ('CMD-TERM', lambda m, o: m.write(int(o['cmdline'], 0), b'x' * 2048))]
            for rule, corrupt in controls:
                got = {x.rule for x in judge(out, image, dump, corrupt=corrupt)}
                results.append(dict(case=f'control-{rule}', rules=sorted(got), ok=rule in got))
        dump.unlink(missing_ok=True)
    return results


def header_mutations():
    """(name, model code, mutate(bytearray) -> bytes) — one per parse_bzimage rejection."""
    def at(off, data):
        def f(d):
            d[off:off + len(data)] = data
            return bytes(d)
        return f
    return [('truncated-header', lambda d: bytes(d[:0x267])),
            ('boot-flag', at(0x1FE, b'\x55\xab')),
            ('magic', at(0x202, b'HdrX')),
            ('version-2.11', at(0x206, struct.pack('<H', 0x020B))),
            ('jump-opcode', at(0x200, b'\xe9')),
            ('header-end-low', at(0x201, b'\x65')),
            ('header-end-high', at(0x201, b'\x8f')),
            ('not-loaded-high', at(0x211, b'\x00')),
            ('not-64bit', at(0x236, struct.pack('<H', 0x7E))),
            ('alignment-not-pow2', at(0x230, struct.pack('<I', 0x300000))),
            ('alignment-zero', at(0x230, struct.pack('<I', 0))),
            ('init-size-zero', at(0x260, struct.pack('<I', 0))),
            ('fixed-pref-zero', lambda d: at(0x258, bytes(8))(bytearray(at(0x234, b'\x00')(d)))),
            ('truncated-image', lambda d: bytes(d[:((d[0x1F1] or 4) + 1) * 512 + struct.unpack_from('<I', d, 0x1F4)[0] * 16 - 1])),
            ('syssize-huge', at(0x1F4, struct.pack('<I', 0xFFFFFFFF))),
            ('payload-outside', lambda d: at(0x248, struct.pack('<II', struct.unpack_from('<I', d, 0x1F4)[0] * 16 - 0x10, 0x11))(d))]


def model_code(image):
    try:
        bp.parse_bzimage(image)
        return None
    except bp.BzImageError as e:
        return e.code


def rejections(host, work, kernel, initrd):
    results = []
    for base_name, base in (('debian', kernel.read_bytes()), ('synthetic', synthetic())):
        for name, mutate in header_mutations():
            bad = mutate(bytearray(base))
            want = model_code(bad)
            code, _, err, dump = load(host, work, f'{base_name}-{name}', bad)
            got = err.split('load refused: ')[-1].split(':')[0] if 'load refused: ' in err else None
            results.append(dict(case=f'{base_name}-{name}', model=want, driver=got, ok=code != 0 and want is not None and got == want))
            dump.unlink(missing_ok=True)
    loader = [('ram-below-map', kernel, ['--ram', 4 * MIB], 'BAD_LAYOUT'),
              ('ram-not-2mib', kernel, ['--ram', 0x3000000 + 4096], 'BAD_LAYOUT'),
              ('kernel-window', kernel, ['--ram', 64 * MIB], 'KERNEL_DOES_NOT_FIT'),
              ('initrd-room', kernel, ['--ram', 96 * MIB, '--initrd', initrd], 'INITRD_DOES_NOT_FIT'),
              ('cmdline-size', kernel, ['--cmdline', 'x' * 2048], 'CMDLINE_TOO_LONG'),
              ('tables-outside-nvs', kernel, ['--rsdp', 0x1000], 'BAD_TABLES'),
              ('fixed-over-zero-page', synthetic(reloc=0, align=0, pref=0x10000, init_size=0x1000), ['--ram', 64 * MIB], 'KERNEL_DOES_NOT_FIT'),
              ('fixed-over-gdt', synthetic(reloc=0, align=0, pref=0x18000, init_size=0x1000), ['--ram', 64 * MIB], 'KERNEL_DOES_NOT_FIT'),
              ('pref-overflow', synthetic(pref=2**64 - 0x1000), [], 'KERNEL_DOES_NOT_FIT')]
    for name, image, extra, want in loader:
        code, _, err, dump = load(host, work, name, image, *extra)
        got = err.split('load refused: ')[-1].split(':')[0] if 'load refused: ' in err else err[-200:]
        results.append(dict(case=name, want=want, driver=got, ok=code != 0 and got == want))
        dump.unlink(missing_ok=True)
    return results


def main():
    a = argparse.ArgumentParser()
    a.add_argument('--out', required=True, type=pathlib.Path)
    a.add_argument('--only', default='check,rejections')
    a.add_argument('--host', type=pathlib.Path)
    a.add_argument('--kernel', type=pathlib.Path, default=RAW / 'linux')
    a.add_argument('--initrd', type=pathlib.Path, default=RAW / 'initrd.gz')
    o = a.parse_args()
    host = o.host or build()
    o.out.mkdir(parents=True, exist_ok=True)
    report = {}
    for mode in o.only.split(','):
        report[mode] = {'check': check, 'rejections': rejections}[mode](host, o.out / mode, o.kernel, o.initrd)
        bad = [r for r in report[mode] if not r['ok']]
        print(f'{mode}: {len(report[mode]) - len(bad)}/{len(report[mode])} pass')
        for r in bad:
            print('  FAIL', json.dumps(r))
    (o.out / 'l1.json').write_text(json.dumps(report, indent=1))
    sys.exit(1 if any(not r['ok'] for v in report.values() for r in v) else 0)


if __name__ == '__main__':
    main()

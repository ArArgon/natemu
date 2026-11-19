"""tools/io/fio-prep/closure.py: Debian version order, the dependency check, guest link resolution and the ELF reader."""
import importlib.util
import pathlib
import struct
import tempfile
import unittest

_spec = importlib.util.spec_from_file_location('fio_prep_closure', pathlib.Path(__file__).with_name('io') / 'fio-prep' / 'closure.py')
closure = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(closure)


def tiny_elf(needed: list, runpath: str | None, interp: str | None) -> bytes:
    """A minimal ELF64 with one PT_LOAD over the whole file, a PT_DYNAMIC and optionally a PT_INTERP."""
    strtab = b'\0'; offs = []
    for s in needed + ([runpath] if runpath else []):
        offs.append(len(strtab)); strtab += s.encode() + b'\0'
    phnum = 3 if interp else 2
    ph_off, data_off = 64, 64 + 56 * phnum
    interp_b = (interp.encode() + b'\0') if interp else b''
    str_off = data_off + len(interp_b)
    dyn = [(1, o) for o in offs[:len(needed)]] + ([(29, offs[-1])] if runpath else []) + [(5, str_off), (0, 0)]
    dyn_off = str_off + len(strtab)
    total = dyn_off + 16 * len(dyn)
    hdr = b'\x7fELF\x02\x01\x01' + b'\0' * 9 + struct.pack('<HHIQQQIHHHHHH', 3, 62, 1, 0, ph_off, 0, 0, 64, 56, phnum, 0, 0, 0)
    ph = struct.pack('<IIQQQQQQ', 1, 5, 0, 0, 0, total, total, 0x1000) + struct.pack('<IIQQQQQQ', 2, 6, dyn_off, dyn_off, 0, 16 * len(dyn), 16 * len(dyn), 8)
    if interp: ph += struct.pack('<IIQQQQQQ', 3, 4, data_off, data_off, 0, len(interp_b), len(interp_b), 1)
    return hdr + ph + interp_b + strtab + b''.join(struct.pack('<qQ', t, v) for t, v in dyn)


class Closure(unittest.TestCase):
    def test_debian_version_order(self):
        for a, b in (('1.0~rc1', '1.0'), ('1.0', '1.0-1'), ('2.41-12', '2.41-12+deb13u4'), ('2.0', '1:1.0'), ('3.5.7-1~deb13u2', '3.5.7-1')):
            self.assertEqual(closure.compare(a, b), -1, (a, b)); self.assertEqual(closure.compare(b, a), 1, (b, a))
        self.assertEqual(closure.compare('1.02.205-2', '01.02.205-2'), 0)

    def test_unmet_dependency_is_reported(self):
        records = {'fio': {'Version': '3.39-1', 'Depends': 'libc6 (>= 2.38), libaio1t64 | libaio1'},
                   'libc6': {'Version': '2.36-9'}}
        r = closure.dependency_check(records)
        self.assertEqual([(row['package'], row['clause']) for row in r['unsatisfied']],
                         [('fio', 'libc6 (>= 2.38)'), ('fio', 'libaio1t64 | libaio1')])
        records['libc6']['Version'] = '2.41-12'; records['libaio1t64'] = {'Version': '0.3.113-8'}
        self.assertEqual(closure.dependency_check(records)['unsatisfied'], [])

    def test_guest_links_never_leave_the_root(self):
        with tempfile.TemporaryDirectory() as t:
            root = pathlib.Path(t)
            (root / 'usr/lib').mkdir(parents=True); (root / 'usr/lib/libx.so.1.2').write_text('x')
            (root / 'lib').symlink_to('usr/lib'); (root / 'usr/lib/libx.so.1').symlink_to('/lib/libx.so.1.2')
            self.assertEqual(closure.guest(root, '/lib/libx.so.1'), root / 'usr/lib/libx.so.1.2')

    def test_elf_dynamic_reads_needed_runpath_and_interpreter(self):
        data = tiny_elf(['libz.so.1', 'libc.so.6'], '$ORIGIN/ceph', '/lib64/ld-linux-x86-64.so.2')
        self.assertEqual(closure.elf_dynamic(data), (['libz.so.1', 'libc.so.6'], ['$ORIGIN/ceph'], '/lib64/ld-linux-x86-64.so.2'))
        self.assertEqual(closure.elf_dynamic(tiny_elf([], None, None)), ([], [], None))

    def test_closure_follows_runpath_and_reports_missing(self):
        with tempfile.TemporaryDirectory() as t:
            root = pathlib.Path(t); lib = root / 'usr/lib/x86_64-linux-gnu'; (lib / 'ceph').mkdir(parents=True); (root / 'usr/bin').mkdir()
            (root / 'usr/bin/fio').write_bytes(tiny_elf(['librbd.so.1'], None, None))
            (lib / 'librbd.so.1').write_bytes(tiny_elf(['libceph-common.so.2', 'libgone.so'], '$ORIGIN/ceph', None))
            (lib / 'ceph/libceph-common.so.2').write_bytes(tiny_elf([], None, None))
            c = closure.elf_closure(root)
            self.assertEqual(list(c['objects']), ['/usr/bin/fio', '/usr/lib/x86_64-linux-gnu/librbd.so.1', '/usr/lib/x86_64-linux-gnu/ceph/libceph-common.so.2'])
            self.assertEqual(c['missing'], [('/usr/lib/x86_64-linux-gnu/librbd.so.1', 'libgone.so')])


if __name__ == '__main__': unittest.main()

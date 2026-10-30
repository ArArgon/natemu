"""Negative controls for acpi_model.py. Run: python3 tools/models/test_acpi_model.py  (or python3 -m unittest).

Uses an N=2 loader dump: $L2_DUMP, else one made with target/release/natemu-host (skipped when neither exists).
Each control corrupts one thing in memory, re-checksums the touched table, and requires the matching rule."""
import os
import pathlib
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from models import acpi_model as am  # noqa: E402

KERNEL = ROOT.parent / 'natemu-raw/kernel-census-20261028-01/dl/linux'
HOST = ROOT / 'target/release/natemu-host'


def dump_n2():
    if os.environ.get('L2_DUMP'):
        return pathlib.Path(os.environ['L2_DUMP']).read_bytes()
    if not (HOST.exists() and KERNEL.exists()):
        raise unittest.SkipTest('no L2_DUMP and no natemu-host/kernel to make one')
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / 'n2.dump'
        subprocess.run([HOST, 'unused', 'boot-linux', 'unused', KERNEL, '--ram', '0x8000000', '--load-only',
                        '--vcpus', '2', '--dump', p], check=True, capture_output=True, timeout=300)
        return p.read_bytes()


class Controls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dump = dump_n2()
        img = am.Image(cls.dump)
        rsdp = struct.unpack('<Q', img.read(am.ZP + 0x70, 8))[0]
        xsdt = struct.unpack_from('<Q', img.read(rsdp, 36), 24)[0]
        x = img.read(xsdt, 52)
        ptrs = {img.read(p, 4): p for p in struct.unpack_from('<2Q', x, 36)}
        fadt = ptrs[b'FACP']
        cls.t = dict(xsdt=xsdt, fadt=fadt, madt=ptrs[b'APIC'],
                     dsdt=struct.unpack_from('<I', img.read(fadt, 44), 40)[0])

    def run_with(self, table, off, data):
        """Writes data at table+off, fixes that table's checksum, returns the violation rules."""
        def corrupt(img):
            a = self.t[table]
            img.write(a + off, data)
            ln = struct.unpack('<I', img.read(a + 4, 4))[0]
            b = bytearray(img.read(a, ln))
            b[9] = 0
            b[9] = -sum(b) & 0xFF
            img.write(a, bytes(b))
        return {v.split(':')[0] for v in am.check(self.dump, 2, corrupt=corrupt)['violations']}

    def test_clean(self):
        self.assertEqual(am.check(self.dump, 2)['violations'], [])

    def test_checksum(self):
        got = {v.split(':')[0] for v in am.check(self.dump, 2, corrupt=lambda img: img.write(
            self.t['xsdt'] + 24, b'\x02'))['violations']}
        self.assertIn('CHECKSUM', got)

    def test_madt_apic_id(self):
        self.assertIn('MADT-APIC', self.run_with('madt', 44 + 8 + 3, b'\x05'))   # second LAPIC's APIC ID

    def test_mailbox_alignment(self):
        r = am.check(self.dump, 2)['info']['MADT_structures']['wake'][0]
        self.assertIn('MAILBOX', self.run_with('madt', r['off'] + 8, struct.pack('<Q', r['mailbox'] + 0x800)))

    def test_aml_pkglength(self):
        # Scope(\_SB) is the first PkgLength after _S5_: raise its high byte so it ends past the DSDT.
        img = am.Image(self.dump)
        d = img.read(self.t['dsdt'], 64)
        i = d.index(b'\x10', 36)
        self.assertEqual(d[i + 1] >> 6, 1)
        self.assertIn('AML', self.run_with('dsdt', i + 2, bytes([d[i + 2] + 1])))

    def test_aml_bad_nameseg(self):
        img = am.Image(self.dump)
        i = img.read(self.t['dsdt'], 64).index(b'_S5_')
        self.assertIn('AML', self.run_with('dsdt', i, b'_s5_'))

    def test_pointer(self):
        self.assertIn('POINTER', self.run_with('xsdt', 44, struct.pack('<Q', self.t['madt'] + 0x10)))

    def test_s3_advertised(self):
        img = am.Image(self.dump)
        i = img.read(self.t['dsdt'], 64).index(b'_S5_')
        self.assertIn('SLEEP', self.run_with('dsdt', i, b'_S3_'))

    def madt(self):
        return am.check(self.dump, 2)['info']['MADT_structures']

    def dsdt_index(self, needle):
        img = am.Image(self.dump)
        ln = struct.unpack('<I', img.read(self.t['dsdt'] + 4, 4))[0]
        i = img.read(self.t['dsdt'], ln).find(needle)
        self.assertGreater(i, 0, f'{needle.hex()} not in the DSDT')
        return i

    def sci_override(self):
        s = [x for x in self.madt()['isos'] if x['src'] == 9]
        self.assertEqual(len(s), 1, 'no IRQ9 override in this dump (builder change not landed?)')
        return s[0]['off']

    def test_route_missing_override(self):
        self.assertIn('ROUTE', self.run_with('madt', self.sci_override() + 3, b'\x0a'))     # source IRQ 9 -> 10

    def test_route_wrong_polarity(self):
        self.assertIn('ROUTE', self.run_with('madt', self.sci_override() + 8, struct.pack('<H', 0x000F)))

    def test_uid_mismatch(self):
        self.assertIn('UID', self.run_with('madt', self.madt()['procs'][1]['off'] + 2, b'\x05'))

    def test_com1_moved(self):
        i = self.dsdt_index(bytes.fromhex('4701f803f803'))
        self.assertIn('DEVICES', self.run_with('dsdt', i, bytes.fromhex('4701f802f802')))

    def test_rtc_missing(self):
        i = self.dsdt_index(bytes.fromhex('0c41d00b00'))                                    # EisaId("PNP0B00")
        self.assertIn('DEVICES', self.run_with('dsdt', i, bytes.fromhex('0c41d00b01')))

    def test_reset_value(self):
        self.assertIn('RESET', self.run_with('fadt', 128, b'\x05'))

    def test_n_mismatch(self):
        self.assertIn('MADT-APIC', {v.split(':')[0] for v in am.check(self.dump, 3)['violations']})

    def test_tmr_val_ext(self):
        img = am.Image(self.dump)
        f = struct.unpack('<I', img.read(self.t['fadt'] + 112, 4))[0]
        self.assertIn('FADT', self.run_with('fadt', 112, struct.pack('<I', f | 1 << 8)))

    def test_nmi_lint0(self):
        o = [x for x in self.madt()['nmis'] if x[0] == 4]
        self.assertTrue(o, 'no local APIC NMI structure')
        self.assertIn('MADT-NMI', self.run_with('madt', o[0][2] + 5, b'\x00'))

    def test_c2_latency(self):
        self.assertIn('IDLE', self.run_with('fadt', 96, struct.pack('<H', 50)))


if __name__ == '__main__':
    unittest.main()

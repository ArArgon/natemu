"""qemu-img convert reporting and the free-space check (io_common), and run_io5's console-only checks."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import pathlib, shutil, subprocess, sys, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import io_common, run_io5


class Convert(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.t, True)

    def test_stderr_reported(self):
        with self.assertRaisesRegex(RuntimeError, 'No such file'): io_common.qcow2_convert(self.t / 'none.qcow2', self.t / 'v.raw', 'raw')

    def test_overlay_converts_sparse_and_shortfall_message(self):
        q = io_common.need_qemu_img(); base, ov = self.t / 'base.raw', self.t / 'ov.qcow2'
        subprocess.run(['truncate', '-s', '64M', base], check=True)
        io_common.overlay_create(base, ov, 1 << 30)
        subprocess.run([q.replace('qemu-img', 'qemu-io'), '-f', 'qcow2', '-c', 'write -P 0x5a 0 16M', ov], check=True, capture_output=True)
        self.assertLess(0, io_common.convert_need(ov))
        io_common.qcow2_convert(ov, self.t / 'v.raw', 'raw')
        self.assertEqual((self.t / 'v.raw').stat().st_size, 1 << 30); self.assertLess((self.t / 'v.raw').stat().st_blocks * 512, 64 << 20)
        real = shutil.disk_usage
        shutil.disk_usage = lambda p: type('U', (), {'free': 100 << 20})()
        try:
            with self.assertRaisesRegex(RuntimeError, 'not enough free disk space.*need ~.*have 0.1 GiB'): io_common.qcow2_convert(ov, self.t / 'w.raw', 'raw')
        finally: shutil.disk_usage = real
        self.assertFalse((self.t / 'w.raw').exists())


class Console(unittest.TestCase):
    def test_console_checks(self):
        t = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, t, True)
        (t / 'serial-verify.log').write_text('[1.0] EXT4-fs error (device vda1): boom\r\nIO5jerr kernel: oops\r\n[2.0] EXT4-fs (vda1): recovery complete\n[3.0] fine\n')
        got = run_io5.console_checks(t, 'verify')
        self.assertEqual(len(got), 3, got)
        (t / 'serial-use.log').write_text('[3.0] fine\n'); self.assertEqual(run_io5.console_checks(t, 'use'), [])

    def test_i386_check_is_judged(self):
        """--i386's first boot passes with the reference's check hash, package list and x87 lines; a wrong hash, a wrong
        package list, an x87 value too far from the reference's or a missing x87 line each fail it."""
        t = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, t, True)
        (t / 'stderr-use.log').write_text('')
        x87 = run_io5.I386_X87.strip().splitlines()

        def problems(check=run_io5.I386_CHECK, packages=run_io5.I386_PACKAGES, lines=x87):
            steps = ''.join(f'IO5 step {s} ok 1.0 start=0.0\r\n' for s in run_io5.REQUIRED_I386)
            (t / 'serial-use.log').write_text(f'IO5 system running boot=1s\r\n{steps}IO5 i386-packages {packages}\r\n'
                'IO5 i386-userland i686 i386 i686-linux-gnu\r\n' + ''.join(f'IO5chk {l}\r\n' for l in lines) +
                f'IO5 i386-check {check}\r\nreboot: Power down\r\n')
            return run_io5.judge(t, 'use', 0, False, True)['problems']
        self.assertEqual(problems(), [])
        self.assertEqual(len(problems(check='0' * 16)), 1)
        self.assertEqual(len(problems(packages='0' * 16)), 1)
        near = [l.replace('1.4142135623730951', '1.414213562373095') for l in x87]; self.assertNotEqual(near, x87)
        self.assertEqual(problems(lines=near), [])                    # one unit in the last place: natemu's doubles
        far = [l.replace('1.4142135623730951', '1.41421356237') for l in x87]
        self.assertEqual(len(problems(lines=far)), 1)
        self.assertEqual(len(problems(lines=x87[1:])), 1)
        self.assertEqual(len(problems(lines=[x87[0].replace('log', 'exp')] + x87[1:])), 1)


if __name__ == '__main__': unittest.main()

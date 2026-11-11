"""The suite's VM lock and `/usr/bin/lockf -k` (and tools/vmlock.py) exclude each other as intended: shared with shared yes,
exclusive with anything no. All of them are flock(2); POSIX record locks (fcntl.lockf) would not exclude (macOS)."""
import fcntl, os, pathlib, subprocess, sys, tempfile, time, unittest

TOOLS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import run_suite  # noqa: E402


class VmLock(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory(); self.path = pathlib.Path(self.d.name, '.vm.lock'); self.path.touch()
        self.suite = run_suite.Suite.__new__(run_suite.Suite)     # only its lock
        self.suite.vmlock = os.open(self.path, os.O_RDWR)
    def tearDown(self): os.close(self.suite.vmlock); self.d.cleanup()

    def lockf(self, *cmd, wait=0):      # `lockf -k -t 0`: fails (75) at once when the lock is taken
        return subprocess.run(['/usr/bin/lockf', '-k', '-t', str(wait), self.path, *cmd]).returncode

    def test_suite_shared_blocks_lockf_exclusive(self):
        fcntl.flock(self.suite.vmlock, fcntl.LOCK_SH)
        self.assertEqual(self.lockf('true'), 75)
    def test_suite_exclusive_blocks_lockf(self):
        self.suite.flock(fcntl.LOCK_EX)
        self.assertEqual(self.lockf('true'), 75)
    def test_lockf_blocks_suite(self):
        p = subprocess.Popen(['/usr/bin/lockf', '-k', self.path, 'sleep', '3']); time.sleep(0.5)
        for mode in (fcntl.LOCK_SH, fcntl.LOCK_EX):
            with self.assertRaises(BlockingIOError): fcntl.flock(self.suite.vmlock, mode | fcntl.LOCK_NB)
        p.wait()
        fcntl.flock(self.suite.vmlock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    def test_shared_with_shared_but_not_exclusive(self):
        fcntl.flock(self.suite.vmlock, fcntl.LOCK_SH)
        p = subprocess.Popen([sys.executable, TOOLS / 'vmlock.py', 'shared', 'sleep', '3'], env=dict(os.environ, NATEMU_RAW_ROOT=self.d.name)); time.sleep(1)
        other = os.open(self.path, os.O_RDWR)
        fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)            # shared holders coexist
        with self.assertRaises(BlockingIOError): fcntl.flock(self.suite.vmlock, fcntl.LOCK_EX | fcntl.LOCK_NB)   # (the other holds shared too)
        os.close(other); p.wait()


if __name__ == '__main__': unittest.main()

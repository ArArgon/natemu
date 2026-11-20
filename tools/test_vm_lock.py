"""The VM lock as the framework's scheduler takes it (testkit.sched.pool.vm_lock: flock on the file), `/usr/bin/lockf -k`
and tools/vmlock.py exclude each other as intended: shared with shared yes, exclusive with anything no. All of them are
flock(2); POSIX record locks (fcntl.lockf) would not exclude (macOS)."""
import fcntl, os, pathlib, subprocess, sys, tempfile, time, unittest

TOOLS = pathlib.Path(__file__).resolve().parent


class VmLock(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory(); self.path = pathlib.Path(self.d.name, '.vm.lock'); self.path.touch()
        self.vmlock = os.open(self.path, os.O_RDWR)
    def tearDown(self): os.close(self.vmlock); self.d.cleanup()

    def lockf(self, *cmd, wait=0):      # `lockf -k -t 0`: fails (75) at once when the lock is taken
        return subprocess.run(['/usr/bin/lockf', '-k', '-t', str(wait), self.path, *cmd]).returncode

    def test_shared_blocks_lockf_exclusive(self):
        fcntl.flock(self.vmlock, fcntl.LOCK_SH)
        self.assertEqual(self.lockf('true'), 75)
    def test_exclusive_blocks_lockf(self):
        fcntl.flock(self.vmlock, fcntl.LOCK_EX)
        self.assertEqual(self.lockf('true'), 75)
    def held_elsewhere(self, p, what):
        """Wait until another process holds the lock (an exclusive try fails), not a fixed time: under a full machine a
        process's start-up can take longer than any fixed sleep."""
        deadline = time.time() + 30
        while True:
            try: fcntl.flock(self.vmlock, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(self.vmlock, fcntl.LOCK_UN)
            except BlockingIOError: return
            self.assertIsNone(p.poll(), f'{what} exited without taking the lock')
            self.assertLess(time.time(), deadline, f'{what} never took the lock'); time.sleep(0.05)

    def test_lockf_blocks_flock(self):
        p = subprocess.Popen(['/usr/bin/lockf', '-k', self.path, 'sleep', '4']); self.addCleanup(p.kill)
        self.held_elsewhere(p, 'lockf')
        for mode in (fcntl.LOCK_SH, fcntl.LOCK_EX):
            with self.assertRaises(BlockingIOError): fcntl.flock(self.vmlock, mode | fcntl.LOCK_NB)
        p.wait()
        fcntl.flock(self.vmlock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    def test_vmlock_py_holds_the_lock(self):
        # a private queue too: vmlock.py queues in the framework's scheduler first (never the machine's real one here),
        # and that queue does not wait for the machine's real VMs: an exclusive grant waits while any natemu or QEMU
        # process runs outside its pool (testkit.sched.pool.outside_vms), which every VM of a running test run is
        env = dict(os.environ, NATEMU_RAW_ROOT=self.d.name, NATEMU_TEST_CACHE=os.path.join(self.d.name, 'cache'),
                   TESTKIT_IGNORE_OUTSIDE_VMS='1')
        for k in ('NATEMU_STATE_ROOT', 'NATEMU_VM_LOCK_HELD', 'TESTKIT_CONTROLLER_PID'): env.pop(k, None)
        for mode, other_shared_ok in (('shared', True), ('exclusive', False)):
            # it holds the lock once it has started and queued: wait for that, not a fixed time
            p = subprocess.Popen([sys.executable, TOOLS / 'vmlock.py', mode, 'sleep', '4'], env=env); self.addCleanup(p.kill)
            self.held_elsewhere(p, f'vmlock.py {mode}')
            with self.assertRaises(BlockingIOError): fcntl.flock(self.vmlock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try: fcntl.flock(self.vmlock, fcntl.LOCK_SH | fcntl.LOCK_NB); shared_ok = True
            except BlockingIOError: shared_ok = False
            self.assertEqual(shared_ok, other_shared_ok)
            fcntl.flock(self.vmlock, fcntl.LOCK_UN)
            p.wait()
            fcntl.flock(self.vmlock, fcntl.LOCK_EX | fcntl.LOCK_NB)       # released when it exits
            fcntl.flock(self.vmlock, fcntl.LOCK_UN)


if __name__ == '__main__': unittest.main()

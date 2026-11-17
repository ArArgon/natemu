"""The suite holds LOCK_EX on natemu-raw/.vm.lock through its serial phase and passes NATEMU_VM_LOCK_HELD=1 to every step after
phase 1. A serial step's runner that takes that lock itself must skip it when told (NATEMU_VM_LOCK_HELD, or the runner's own
--no-lock): its flock would otherwise wait forever on the parent's separate open file description."""
import inspect, pathlib, re, sys, unittest
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import run_suite


class SerialStepsDoNotLock(unittest.TestCase):
    def test_suite_passes_lock_held(self):
        self.assertIn("'NATEMU_VM_LOCK_HELD': '1'", inspect.getsource(run_suite.Suite.run))

    def test_serial_runners(self):
        bad = []
        for s in run_suite.steps(pathlib.Path('/x/fexc'), pathlib.Path('/x/out')):
            if s.phase != 'ser' or '--no-lock' in s.argv: continue    # --no-lock: the runner's own opt-out (virtio-lifecycle-timed)
            for a in s.argv:
                p = pathlib.Path(run_suite.ROOT) / a
                if a.startswith('tools/') and a.endswith('.py') and p.is_file():
                    src = p.read_text()
                    if re.search(r'flock\(', src) and re.search(r'VM_LOCK|\.vm\.lock', src) and 'NATEMU_VM_LOCK_HELD' not in src:
                        bad.append(f'{s.name}: {a}')
        self.assertEqual(bad, [], 'serial steps whose runner takes the VM lock without honouring NATEMU_VM_LOCK_HELD (deadlocks under the suite lock)')


if __name__ == '__main__': unittest.main()

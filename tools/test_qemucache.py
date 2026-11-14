"""tools/qemucache.py: finished runs are remembered under a key that follows the image, arguments, CPU model, QEMU binary and
firmware; a timeout only after two fresh ones and only on request; an unreadable row is a miss; the off switch."""
import contextlib
import inspect
import io
import os
import pathlib
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import qemucache

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / 'bench' / 'smp'))


QUEUE = []      # outcomes the next fake QEMU runs give, before the image decides


def fake_smp(root, calls, binary=b'qemu', firmware=b'bios'):
    exe = pathlib.Path(root, 'prefix', 'bin', 'qemu'); exe.parent.mkdir(parents=True, exist_ok=True); exe.write_bytes(binary)
    share = pathlib.Path(root, 'prefix', 'share', 'qemu'); share.mkdir(parents=True, exist_ok=True)
    (share / 'bios-256k.bin').write_bytes(firmware)

    def run_image(prog, img, work, mode='multi', timeout=120, extra=(), cpu=None):
        calls.append((img, timeout, cpu))
        rc = QUEUE.pop(0) if QUEUE else {b'hang': 'timeout', b'crash': 2}.get(img[:5], 1)
        return rc, '', (b'dump' + img if rc == 1 else b''), 0.5
    return types.SimpleNamespace(run_image=run_image, QEMU=str(exe), default_cpu=lambda prog: 'sys' if prog.get('system') else 'max',
                                 qemu_args=lambda n, mode, dbg, img, cpu='max', extra=(): ['qemu', str(n), mode, cpu, *extra])


class QemuCache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)
        env = patch.dict(os.environ, {'NATEMU_RAW_ROOT': self.dir.name}); env.start(); self.addCleanup(env.stop)
        os.environ.pop('NATEMU_QEMU_CACHE', None)
        self.calls = []; QUEUE.clear()

    def process(self, **kw):
        smp = fake_smp(self.dir.name, self.calls, **kw); qemucache.install(smp)
        return smp, lambda img, prog={'ncpu': 1}, **k: smp.run_image(prog, img, pathlib.Path(self.dir.name), k.pop('mode', 'multi'), **k)

    def test_finished_runs_extra_arguments_and_crashes(self):
        smp, run = self.process()
        self.assertEqual(run(b'ok')[:3], (1, '', b'dumpok')); self.assertEqual(run(b'ok'), (1, '', b'dumpok', 0.0))
        self.assertEqual(len(self.calls), 1)
        run(b'ok', extra=['-serial', 'file:x']); run(b'ok', extra=['-serial', 'file:x'])      # extra arguments: never cached
        for _ in range(2): self.assertEqual(run(b'crash')[0], 2)                               # a crash: never cached
        for _ in range(2): self.assertEqual(run(b'hang', timeout=10)[0], 'timeout')            # a timeout without hang_ok: never
        self.assertEqual(len(self.calls), 7)
        smp2, run2 = self.process()                                                             # another process reads it back
        self.assertEqual(run2(b'ok')[2], b'dumpok'); self.assertEqual(len(self.calls), 7)

    def test_key_follows_image_arguments_cpu_binary_and_firmware(self):
        _, run = self.process(); run(b'ok')
        n = len(self.calls)
        for what, call in {'image': lambda r: r(b'ok2'), 'cpus': lambda r: r(b'ok', prog={'ncpu': 2}), 'mode': lambda r: r(b'ok', mode='single'),
                           'default cpu': lambda r: r(b'ok', prog={'ncpu': 1, 'system': True}), 'cpu model': lambda r: r(b'ok', cpu='Haswell')}.items():
            call(run); n += 1; self.assertEqual(len(self.calls), n, what)
            call(run); self.assertEqual(len(self.calls), n, what + ' again: a hit')          # and each is cached on its own
        for what, kw in {'binary': {'binary': b'qemu2'}, 'firmware': {'firmware': b'bios2'}}.items():
            _, run2 = self.process(**kw); run2(b'ok'); n += 1; self.assertEqual(len(self.calls), n, what)
            _, run3 = self.process(**kw); run3(b'ok'); self.assertEqual(len(self.calls), n, what + ' again: a hit')

    def test_cpu_model_reaches_qemu_and_the_default_comes_from_smp(self):
        _, run = self.process()
        run(b'ok', cpu='Haswell'); run(b'ok', prog={'ncpu': 1, 'system': True})
        self.assertEqual([c[2] for c in self.calls], ['Haswell', None])       # smp.run_image derives the default itself
        real = __import__('smp')       # the real smp.py accepts the argument and owns the default
        self.assertIn('cpu', inspect.signature(real.run_image).parameters)
        self.assertEqual((real.default_cpu({'system': True}), real.default_cpu({})), (real.SYSTEM_CPU, 'max'))

    def test_a_hang_is_stored_only_after_two_fresh_timeouts(self):
        _, run = self.process()
        hang = lambda **k: run(b'hang', hang_ok=True, **{'timeout': 10, **k})
        for expected in (1, 2):                                      # a strike, then the confirmation: both run QEMU
            self.assertEqual(hang()[0], 'timeout'); self.assertEqual(len(self.calls), expected)
        self.assertEqual(hang(), ('timeout', '', b'', 0.0)); self.assertEqual(hang(timeout=5)[3], 0.0)
        self.assertEqual(len(self.calls), 2)                         # served: confirmed hangs need no QEMU
        hang(timeout=30); self.assertEqual(len(self.calls), 3)       # a longer wait than observed asks again
        self.assertEqual(run(b'hang', timeout=10)[3], 0.5)            # a caller that does not expect a hang is never served one
        self.assertEqual(len(self.calls), 4)

    def test_a_finished_run_after_a_strike_replaces_it(self):
        _, run = self.process()
        QUEUE[:] = ['timeout', 1]
        self.assertEqual(run(b'x', hang_ok=True, timeout=10)[0], 'timeout')      # a strike: not served
        self.assertEqual(run(b'x', hang_ok=True, timeout=10)[0], 1)              # QEMU finished this time: that is the answer
        self.assertEqual(run(b'x', hang_ok=True, timeout=10)[:3], (1, '', b'dumpx')); self.assertEqual(len(self.calls), 2)

    def test_unreadable_row_and_database_are_misses_reported_once(self):
        _, run = self.process(); run(b'ok'); run(b'ok2')
        db = sqlite3.connect(pathlib.Path(self.dir.name, 'qemu-cache.sqlite'))
        db.execute("update q set v = x'00ff'"); db.commit(); db.close()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _, run2 = self.process()
            self.assertEqual(run2(b'ok')[2], b'dumpok'); self.assertEqual(run2(b'ok2')[2], b'dumpok2')
        self.assertEqual(len(self.calls), 4); self.assertEqual(err.getvalue().count('qemucache: unreadable row'), 1)
        _, run3 = self.process(); run3(b'ok'); self.assertEqual(len(self.calls), 4)        # the miss rewrote the row
        for f in pathlib.Path(self.dir.name).glob('qemu-cache.sqlite*'): f.unlink()
        pathlib.Path(self.dir.name, 'qemu-cache.sqlite').write_bytes(b'not a database' * 100)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            _, run4 = self.process(); self.assertEqual(run4(b'ok')[2], b'dumpok')
        self.assertIn('qemucache: database unusable', err.getvalue())

    def test_off_switch(self):
        with patch.dict(os.environ, {'NATEMU_QEMU_CACHE': 'off'}):
            _, run = self.process(); run(b'ok'); run(b'ok')
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(pathlib.Path(self.dir.name, 'qemu-cache.sqlite').exists())


if __name__ == '__main__': unittest.main()

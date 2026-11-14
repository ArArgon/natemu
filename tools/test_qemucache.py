"""tools/qemucache.py: finished runs are remembered, a flaky timeout never, an expected hang only on request."""
import os
import pathlib
import tempfile
import types
import unittest
from unittest.mock import patch

import qemucache


def fake_smp(root, calls):
    qemu = pathlib.Path(root, 'qemu'); qemu.write_bytes(b'qemu')

    def run_image(prog, img, work, mode='multi', timeout=120, extra=()):
        calls.append((img, timeout))
        rc = {b'hang': 'timeout', b'crash': 2}.get(img[:5], 1)
        return rc, '', b'dump' + img if rc == 1 else b'', 0.5
    return types.SimpleNamespace(run_image=run_image, QEMU=str(qemu), SYSTEM_CPU='sys',
                                 qemu_args=lambda n, mode, dbg, img, cpu='max', extra=(): ['qemu', str(n), mode, cpu, *extra])


class QemuCache(unittest.TestCase):
    def test_cache(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'NATEMU_RAW_ROOT': root}):
            calls = []
            smp = fake_smp(root, calls); qemucache.install(smp)
            prog, work = {'ncpu': 1}, pathlib.Path(root)
            run = lambda img, **kw: smp.run_image(prog, img, work, 'multi', **kw)
            self.assertEqual(run(b'ok')[:3], (1, '', b'dumpok')); self.assertEqual(run(b'ok'), (1, '', b'dumpok', 0.0))
            self.assertEqual(len(calls), 1)
            run(b'ok', extra=['-serial', 'file:x']); run(b'ok', extra=['-serial', 'file:x'])      # extra arguments: never cached
            self.assertEqual(len(calls), 3)
            for _ in range(2): self.assertEqual(run(b'crash')[0], 2)                               # a crash: never cached
            self.assertEqual(len(calls), 5)
            for _ in range(2): self.assertEqual(run(b'hang', timeout=10)[0], 'timeout')            # a timeout: not without hang_ok
            self.assertEqual(len(calls), 7)
            run(b'hang', timeout=10, hang_ok=True); run(b'hang', timeout=10, hang_ok=True); run(b'hang', timeout=5, hang_ok=True)
            self.assertEqual(len(calls), 8)                                                         # remembered for a limit up to its own
            run(b'hang', timeout=30, hang_ok=True)
            self.assertEqual(len(calls), 9)                                                         # a longer wait asks QEMU again
            smp2 = fake_smp(root, calls); qemucache.install(smp2)                                    # another process: read back
            self.assertEqual(smp2.run_image(prog, b'ok', work)[2], b'dumpok')
            self.assertEqual(len(calls), 9)


if __name__ == '__main__': unittest.main()

"""tools/asmcache.py: one real assembly per distinct text, in a process and across processes; errors are not cached."""
import os
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

import asmcache


def fake_ref(calls):
    def assemble(text, labels=False):
        calls.append((text, labels))
        if text == 'bad': raise ValueError('assembler: bad')
        return (text.encode(), {'l': 3}) if labels else text.encode()
    return types.SimpleNamespace(assemble=assemble)


class AsmCache(unittest.TestCase):
    def test_memo_disk_and_errors(self):
        clang = subprocess.CompletedProcess([], 0, stdout='clang version test\n')
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'NATEMU_RAW_ROOT': root}), \
             patch.object(asmcache.subprocess, 'run', return_value=clang):
            calls = []
            ref = fake_ref(calls); flush = asmcache.install(ref)
            self.assertEqual(ref.assemble('nop'), b'nop'); self.assertEqual(ref.assemble('nop'), b'nop')
            self.assertEqual(ref.assemble('nop', labels=True), (b'nop', {'l': 3}))
            for _ in range(2):
                with self.assertRaises(ValueError): ref.assemble('bad')
            self.assertEqual(calls, [('nop', False), ('nop', True), ('bad', False), ('bad', False)])
            flush()
            calls2 = []        # a second process: the rows are read back, the failing text is assembled again
            ref2 = fake_ref(calls2); asmcache.install(ref2)
            self.assertEqual(ref2.assemble('nop'), b'nop')
            self.assertEqual(ref2.assemble('nop', labels=True), (b'nop', {'l': 3}))
            with self.assertRaises(ValueError): ref2.assemble('bad')
            self.assertEqual(calls2, [('bad', False)])
            self.assertIs(asmcache.install(ref2), ref2.assemble.flush)      # installing twice wraps once


if __name__ == '__main__': unittest.main()

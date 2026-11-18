"""tools/asmcache.py: one real assembly per distinct text, in a process and across processes; the key follows clang and the
assembler's source; errors are not cached; an unreadable row or database is a miss; NATEMU_ASM_CACHE=off turns it off."""
import contextlib
import io
import os
import pathlib
import sqlite3
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

import asmcache

CALLS = []


def assemble_a(text, labels=False):
    CALLS.append((text, labels))
    if text == 'bad': raise ValueError('assembler: bad')
    return (text.encode(), {'l': 3}) if labels else text.encode()


def assemble_b(text, labels=False):        # a different source: the key must differ
    CALLS.append((text, labels))
    return (text.encode(), {'l': 3}) if labels else text.encode()


def clang(version): return subprocess.CompletedProcess([], 0, stdout=f'clang version {version}\n')


class AsmCache(unittest.TestCase):
    def setUp(self):
        CALLS.clear()
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)
        env = patch.dict(os.environ, {'NATEMU_RAW_ROOT': self.dir.name + '/inputs', 'NATEMU_STATE_ROOT': self.dir.name}); env.start(); self.addCleanup(env.stop)
        os.environ.pop('NATEMU_ASM_CACHE', None)

    def process(self, assemble=assemble_a, version='1'):
        """A fresh 'process': a new wrapped ref, flushed when `done()` is called."""
        ref = types.SimpleNamespace(assemble=assemble)
        with patch.object(asmcache.subprocess, 'run', return_value=clang(version)):
            flush = asmcache.install(ref)
            ref.assemble('x')            # (the clang version is read on first use, under the patch)
        CALLS.clear()
        return ref, flush

    def test_memo_disk_and_errors(self):
        ref, flush = self.process()
        self.assertEqual(ref.assemble('nop'), b'nop'); self.assertEqual(ref.assemble('nop'), b'nop')
        self.assertEqual(ref.assemble('nop', labels=True), (b'nop', {'l': 3}))
        for _ in range(2):
            with self.assertRaises(ValueError): ref.assemble('bad')
        self.assertEqual(CALLS, [('nop', False), ('nop', True), ('bad', False), ('bad', False)])
        flush()
        ref2, _ = self.process()
        self.assertEqual(ref2.assemble('nop'), b'nop'); self.assertEqual(ref2.assemble('nop', labels=True), (b'nop', {'l': 3}))
        with self.assertRaises(ValueError): ref2.assemble('bad')        # an assembler error is still an error, never a cached row
        self.assertEqual(CALLS, [('bad', False)])
        self.assertIs(asmcache.install(ref2), ref2.assemble.flush)       # installing twice wraps once

    def test_key_follows_clang_and_the_assemblers_source(self):
        ref, flush = self.process(); ref.assemble('nop'); flush()
        for kwargs in ({'version': '2'}, {'assemble': assemble_b}):
            ref2, _ = self.process(**kwargs)
            ref2.assemble('nop')
            self.assertEqual(CALLS, [('nop', False)], kwargs)
            CALLS.clear()
        ref3, _ = self.process(); ref3.assemble('nop'); self.assertEqual(CALLS, [])      # the same clang and source: a hit

    def test_off_switch(self):
        with patch.dict(os.environ, {'NATEMU_ASM_CACHE': 'off'}):
            ref, flush = self.process(); ref.assemble('nop'); flush()
        self.assertFalse(pathlib.Path(self.dir.name, 'asm-cache.sqlite').exists())
        ref2, _ = self.process(); ref2.assemble('nop'); self.assertEqual(CALLS, [('nop', False)])

    def test_corrupt_row_and_database_are_misses(self):
        ref, flush = self.process(); ref.assemble('nop'); ref.assemble('nop', labels=True); flush()
        db = sqlite3.connect(pathlib.Path(self.dir.name, 'asm-cache.sqlite'))
        db.execute("update a set v = 'not json'"); db.commit(); db.close()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ref2, flush2 = self.process()
            self.assertEqual(ref2.assemble('nop'), b'nop'); self.assertEqual(ref2.assemble('nop', labels=True), (b'nop', {'l': 3}))
            with self.assertRaises(ValueError): ref2.assemble('bad')
            flush2()
        self.assertEqual(CALLS, [('nop', False), ('nop', True), ('bad', False)])
        self.assertEqual(err.getvalue().count('asmcache: unreadable row'), 1)       # reported once
        ref3, _ = self.process(); self.assertEqual(ref3.assemble('nop'), b'nop'); self.assertEqual(CALLS, [])    # and repaired
        for f in pathlib.Path(self.dir.name).glob('asm-cache.sqlite*'): f.unlink()
        pathlib.Path(self.dir.name, 'asm-cache.sqlite').write_bytes(b'this is not a database' * 100)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            ref4, _ = self.process(); self.assertEqual(ref4.assemble('nop'), b'nop')
        self.assertIn('asmcache: database unusable', err.getvalue())


if __name__ == '__main__': unittest.main()

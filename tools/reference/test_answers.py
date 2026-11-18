"""tools/reference/answers.py: seed then locked gives the same verdict; locked never runs QEMU; a key the lock lacks is
Missing; a different state fails.   python3 -m unittest discover -s tools/reference -p test_answers.py"""
import os, pathlib, subprocess, sys, tempfile, unittest
from unittest import mock
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE.parent / 'bench' / 'smp'))
import answers, flags, ref, smp

PROG = {'name': 'p', 'code': '4801d8', 'rip': '0x100000', 'gpr': [hex(0x1000 + i) for i in range(16)], 'rflags': '0x0',
        'memory': [{'addr': '0x200000', 'data': '00112233'}], 'insns': [{'op': 'add', 'width': 64}]}
FINAL = {'gpr': [hex(i) for i in range(16)], 'rflags': '0x246', 'rip': '0x100003', 'memory': [{'addr': '0x200000', 'data': '00112233'}]}
MASK = flags.TRACKED & ~flags.AF


def fake_run_programs(programs, workdir, qemu):
    """ref._run_programs without a container: every program ends in FINAL."""
    return [{'name': p['name'], 'qemu': dict(FINAL), 'rflags_compare_mask': hex(MASK)} for p in programs], {'cache_hits': 0}


def no_subprocess(*a, **k): raise AssertionError(f'a process was started in locked mode: {a[:1]}')


class Answers(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log, self.lock = pathlib.Path(self.dir.name, 'answers.log'), pathlib.Path(self.dir.name, 'tests.lock')
        self.env = mock.patch.dict(os.environ, {'NATEMU_ANSWERS_LOG': str(self.log), 'NATEMU_ANSWERS_LOCK': str(self.lock),
                                                'NATEMU_QEMU_PIN': 'qemu-system 11.1.2 src'})
        self.env.start()

    def tearDown(self):
        self.env.stop(); self.dir.cleanup(); answers.MISSING.clear()

    def natemu(self, **change):
        s = {'gpr': [int(x, 0) for x in FINAL['gpr']], 'rflags': 0x256, 'rip': 0x100003, 'memory': [dict(m) for m in FINAL['memory']]}
        return s | change

    def verdict(self, mode, state, programs=(PROG,)):
        with mock.patch.dict(os.environ, {'NATEMU_ANSWERS': mode}), mock.patch.object(ref, '_run_programs', fake_run_programs):
            results, _ = ref.run_programs(list(programs), self.dir.name, '/nonexistent/qemu')
            return ref.compare(results[0], state['gpr'], state['rflags'], state['rip'], state['memory'])

    def seed_and_lock(self, state):
        live = self.verdict('live', state)
        self.assertEqual(self.verdict('seed', state), live)
        answers.write_lock(answers.build_lock([self.log]), self.lock)
        return live

    def test_user_seed_then_locked_same_verdict(self):
        for small in (True, False):
            with self.subTest(inline=small), mock.patch.object(answers, 'VALUE_MAX', 1024 if small else 16):
                self.same_verdicts(inline=small)

    def same_verdicts(self, inline):
        for state in (self.natemu(), self.natemu(rflags=0x247), self.natemu(rip=0x100004),
                      self.natemu(memory=[{'addr': '0x200000', 'data': '00112234'}])):
            with self.subTest(state=state):
                self.log.unlink(missing_ok=True)
                live = self.seed_and_lock(state)
                with mock.patch.object(subprocess, 'run', no_subprocess), mock.patch.object(subprocess, 'Popen', no_subprocess):
                    locked = self.verdict('locked', state)
                self.assertEqual(bool(locked), bool(live))
                if live and inline:     # rebuilt from the inline value: the real diff (RFLAGS shown under the mask)
                    self.assertEqual([d.split(':')[0] for d in locked], [d.split(':')[0] for d in live])
                elif live: self.assertIn('differs from the locked answer (hash only', locked[0])

    def test_user_masked_flag_passes_locked(self):       # AF is outside the mask: a natemu AF difference still passes
        self.seed_and_lock(self.natemu(rflags=0x256))
        self.assertEqual(self.verdict('locked', self.natemu(rflags=0x246)), [])

    def test_locked_missing_key_raises_and_never_runs_qemu(self):
        self.seed_and_lock(self.natemu())
        other = dict(PROG, code='4801c8')
        with mock.patch.object(subprocess, 'run', no_subprocess), mock.patch.object(subprocess, 'Popen', no_subprocess), \
                mock.patch.object(ref, '_run_programs', no_subprocess):
            with mock.patch.dict(os.environ, {'NATEMU_ANSWERS': 'locked'}):
                results, _ = ref.run_programs([other], self.dir.name, '/nonexistent/qemu')
                with self.assertRaises(answers.Missing) as e:
                    ref.compare(results[0], *self.natemu().values())
        self.assertEqual(e.exception.key, results[0].key)

    def test_locked_mismatch_fails(self):
        self.seed_and_lock(self.natemu())
        bad = self.natemu(gpr=[0] * 16)
        self.assertEqual(len(self.verdict('locked', bad)), 15)            # inline: the real field diffs
        self.log.unlink()
        with mock.patch.object(answers, 'VALUE_MAX', 16):
            self.seed_and_lock(self.natemu())
            self.assertEqual(len(self.verdict('locked', bad)), 1)        # hash only: one difference

    def test_locked_field_read_is_refused(self):
        self.seed_and_lock(self.natemu())
        with mock.patch.dict(os.environ, {'NATEMU_ANSWERS': 'locked'}):
            results, _ = ref.run_programs([PROG], self.dir.name, '/nonexistent/qemu')
        self.assertEqual(results[0]['qemu']['rip'], '0x100003')          # inline: rebuilt
        with self.assertRaises(answers.Unavailable): results[0]['fault']   # not part of the projection
        self.log.unlink(); answers._cache.clear()
        with mock.patch.object(answers, 'VALUE_MAX', 16): self.seed_and_lock(self.natemu())
        with mock.patch.dict(os.environ, {'NATEMU_ANSWERS': 'locked'}):
            results, _ = ref.run_programs([PROG], self.dir.name, '/nonexistent/qemu')
        with self.assertRaises(answers.Unavailable): results[0]['qemu']
        self.assertTrue(results[0])          # a placeholder is never falsy ("if want:" must not skip the comparison)

    def test_key_ignores_name_and_backend(self):
        k = lambda p: answers.user_key(ref.norm(p), ref.qemu_args(ref.norm(p)))[0]
        with mock.patch.dict(os.environ, {'NATEMU_REFERENCE_BACKEND': 'docker'}): a = k(PROG)
        with mock.patch.dict(os.environ, {'NATEMU_REFERENCE_BACKEND': 'apple'}): b = k(dict(PROG, name='other'))
        self.assertEqual(a, b)
        self.assertNotEqual(a, k(dict(PROG, cpu='qemu64,+adx')))

    def test_views_and_derived_values(self):
        with mock.patch.dict(os.environ, {'NATEMU_ANSWERS': 'seed'}):
            r = answers.Ref({'x': [1, 2], 'y': 3}, 'K')
            v = answers.view(r, 'only-x', lambda r: {'x': r['x']})
            self.assertEqual((v.key, dict(v)), ('K/only-x', {'x': [1, 2]}))
            self.assertEqual(answers.derive(r, 'sum', lambda r: (sum(r['x']), 'ok')), [3, 'ok'])
            self.assertTrue(answers.equal(r, 'serial', 'abc', 'abc'))
        answers.write_lock(answers.build_lock([self.log]), self.lock)
        with mock.patch.dict(os.environ, {'NATEMU_ANSWERS': 'locked'}):
            r = answers.Ref.placeholder('K')
            self.assertEqual(answers.view(r, 'only-x', None).key, 'K/only-x')
            self.assertEqual(answers.derive(r, 'sum', None), [3, 'ok'])
            self.assertTrue(answers.equal(r, 'serial', 'abc'))
            self.assertFalse(answers.equal(r, 'serial', 'abd'))
            with self.assertRaises(answers.Missing): answers.equal(r, 'other', 'abc')

    def test_corrupt_inline_value_is_refused(self):
        self.seed_and_lock(self.natemu())
        self.lock.write_text(self.lock.read_text().replace('"rip\\":1048579', '"rip\\":1048580'))
        with mock.patch.dict(os.environ, {'NATEMU_ANSWERS': 'locked'}), self.assertRaises(ValueError):
            ref.run_programs([PROG], self.dir.name, '/nonexistent/qemu')

    def test_system_seed_then_locked(self):
        prog = {'name': 's', 'ncpu': 1, 'memory': [{'addr': '0x200000', 'data': '00'}], 'cpus': [{}], 'bodies': []}
        want = {'cpus': [{'gpr': list(range(16)), 'rflags': 0x46}], 'memory': [{'addr': 0x200000, 'data': b'\x01'}]}
        runs = []
        def fake(*a): runs.append(a); return 1, '', b'DUMP', 0.1
        def fake_hang(*a): return 'timeout', '', b'', 30.0
        with mock.patch.object(smp, '_run_image', fake), mock.patch.object(smp, '_parse_dump', lambda prog, data: want):
            with mock.patch.dict(os.environ, {'NATEMU_ANSWERS': 'seed'}):
                rc, err, dump, _ = smp.run_image(prog, b'IMAGE', pathlib.Path(self.dir.name), 'multi')
                seeded = smp.compare(prog, want, smp.parse_dump(prog, dump))
                with mock.patch.object(smp, '_run_image', fake_hang):
                    hang = smp.run_image(prog, b'HANG', pathlib.Path(self.dir.name), hang_ok=True)[0]
                    smp.run_image(prog, b'FLAKY', pathlib.Path(self.dir.name))       # a timeout of a non-hang case: no answer
            answers.write_lock(answers.build_lock([self.log]), self.lock)
            with mock.patch.dict(os.environ, {'NATEMU_ANSWERS': 'locked'}), mock.patch.object(subprocess, 'run', no_subprocess):
                n = len(runs)
                rc2, _, dump2, _ = smp.run_image(prog, b'IMAGE', pathlib.Path(self.dir.name), 'multi')
                self.assertEqual((rc2, len(runs)), (1, n))
                q = smp.parse_dump(prog, dump2)
                self.assertEqual(smp.compare(prog, want, q), seeded)
                other = {'cpus': [{'gpr': list(range(16)), 'rflags': 0x47}], 'memory': want['memory']}
                self.assertEqual(len(smp.compare(prog, other, q)), 1)
                self.assertEqual(smp.run_image(prog, b'HANG', pathlib.Path(self.dir.name), hang_ok=True)[0], hang)
                with self.assertRaises(answers.Missing): smp.run_image(prog, b'FLAKY', pathlib.Path(self.dir.name))
        self.assertEqual((seeded, hang), ([], 'timeout'))

    def test_live_is_unchanged(self):
        os.environ.pop('NATEMU_ANSWERS', None)
        self.assertFalse(answers.active())
        with mock.patch.object(ref, '_run_programs', fake_run_programs):
            results, _ = ref.run_programs([PROG], self.dir.name, '/q')
        self.assertIs(type(results[0]), dict)
        self.assertFalse(self.log.exists())


if __name__ == '__main__': unittest.main()

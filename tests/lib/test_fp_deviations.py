"""Accurate-FP plan chunk 0: the deviation registry, docs/floating-point.md's list of it, the judging rule, and each runner's no-VM self-check."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import json, pathlib, re, sys, unittest
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools/reference'))
import fp_deviations as dev


def readme_list():   # (the list moved from the README to docs/floating-point.md)
    text = (ROOT / 'docs/floating-point.md').read_text()
    body = text.split('## Fast mode on a Mac without FEAT_AFP\n', 1)[1].split('\n#', 1)[0]
    return dict(re.findall(r'^- `([a-z0-9-]+)`: (.*)$', body, re.M))


class Registry(unittest.TestCase):
    def test_readme_lists_the_same_classes_with_the_same_descriptions(self):
        readme = readme_list()
        self.assertEqual(sorted(readme), sorted(dev.CLASSES))
        for name, (_, description, _) in dev.CLASSES.items():
            self.assertEqual(readme[name].rstrip('.'), description, name)

    def test_the_page_names_every_other_x87_deviation(self):
        text = (ROOT / 'docs/floating-point.md').read_text()
        for name in dev.X87_OTHER: self.assertIn(f'`{name}`', text, name)
        self.assertFalse(set(dev.X87_OTHER) & set(dev.CLASSES))

    def test_the_x87_rule(self):
        m2, m4, acc = {'afp': False, 'fp': 'fast'}, {'afp': True, 'fp': 'fast'}, {'afp': False, 'fp': 'accurate'}
        v = lambda r, exact, deviated: dev.x87_verdict('masked-x87-de', r, exact, deviated)[0]
        self.assertEqual(v(m2, ['fsw'], []), 'deviated')
        self.assertEqual(v(m2, [], ['fsw']), 'fail')         # tagged but exact
        self.assertEqual(v(m2, ['fsw'], ['fsw']), 'fail')    # another wrong result
        for r in (m4, acc, {}):
            self.assertEqual(v(r, [], ['fsw']), 'ok')
            self.assertEqual(v(r, ['fsw'], []), 'fail')      # the deviated result where the case is judged exactly
        self.assertEqual(dev.x87_verdict(None, m2, [], ['x']), ('ok', []))

    def test_every_tagged_corpus_program_names_a_registered_class(self):
        for corpus, progs in json.loads(dev.PREDICTIONS.read_text()).items():
            for name, entry in progs.items():
                self.assertTrue(entry['classes'] and set(entry['classes']) <= set(dev.CLASSES), (corpus, name))
        # i8 and i8b are listed apart: their names repeat
        self.assertEqual(sorted(json.loads(dev.PREDICTIONS.read_text())), ['i8-corpus-20261030-01', 'i8-corpus-20261030-02'])


class Rule(unittest.TestCase):
    M2, M4 = {'afp': False, 'fp': 'fast'}, {'afp': True, 'fp': 'fast'}

    def test_the_rule_applies_only_to_a_host_without_afp_in_fast_mode(self):
        self.assertTrue(dev.applies(self.M2))
        for r in (self.M4, {'afp': False, 'fp': 'accurate'}, {'afp': True, 'fp': 'accurate'}, {}, {'afp': False}, {'fp': 'fast'}):
            self.assertFalse(dev.applies(r), r)

    def test_tags_are_derived(self):
        t = lambda start, want, simd=True: dev.tag_mxcsr(simd, start, want)
        self.assertEqual(t(0x1f80, 0x1f82), 'masked-sse-de')
        self.assertIsNone(t(0x1f80, 0x1f80))                 # DE not expected
        self.assertIsNone(t(0x1e80, 0x1e82))                 # DE unmasked: exact in fast mode
        self.assertIsNone(t(0x1f82, 0x1f82))                 # DE preset: DE missing would hide a bug that clears the sticky flag
        self.assertIsNone(t(0x1f80, 0x1f82, simd=False))
        self.assertIsNone(t(0x1f80, (0x1f82, 0x1f83)))       # alternatives are never tagged

    def test_mutations(self):
        t, u = dev.make_mx(0x1f80, 0x1fa2), dev.make_mx(0x1f80, 0x1fa0)
        dev.self_check(t, u, 'unit')
        v = lambda mx, r, got: dev.mxcsr_verdict(mx, r, got)[0]
        self.assertEqual(v(t, self.M2, 0x1fa0), 'deviated')
        self.assertEqual(v(t, self.M2, 0x1fa2), 'fail')      # tagged but exact
        self.assertEqual(v(t, self.M2, 0x1fa3), 'fail')      # an extra flag
        self.assertEqual(v(t, self.M2, 0x1f82), 'fail')      # a different wrong result (a cleared PE)
        self.assertEqual(v(t, self.M2, 0x1f80 | 2), 'fail')  # DE and a cleared PE
        self.assertEqual(v(u, self.M2, 0x1fa0), 'ok')
        self.assertEqual(v(u, self.M2, 0x1fa2), 'fail')      # an untagged failure
        self.assertEqual(v(t, self.M4, 0x1fa2), 'ok')        # the M4 judges strictly
        self.assertEqual(v(t, self.M4, 0x1fa0), 'fail')

    def test_counts_line(self):
        c = dev.Counts()
        for k in ['ok'] * 396 + ['deviated'] * 21:
            c.add(k, 'masked-sse-de')
        self.assertEqual(c.line(), '417 cases, 396 as expected, 0 not, 21 deviated as documented (masked-sse-de)')


class Runners(unittest.TestCase):
    def test_i5a_de(self):
        import run_i5a_de
        run_i5a_de.self_check()

    def test_i5c(self):
        import run_i5c
        run_i5c.self_check()

    def test_i8_faults(self):
        import run_i8_faults
        run_i8_faults.self_check()

    def test_corpora(self):
        """The corpora as gen_i8.py makes them now, in folders named by the predictions' keys, with answers from the model's QEMU
        rules (they reproduce every QEMU answer: predict.py checks it): no raw-results folder is read."""
        import shutil, tempfile, run_slice1_vm
        sys.path.insert(0, str(ROOT / 'tools/models/afp_fp'))
        import predict
        if not shutil.which('clang'): self.skipTest('no clang: gen_i8.py assembles the i8 corpora with it')
        with tempfile.TemporaryDirectory() as tmp:
            for key in predict.CORPORA.values():
                programs = predict.generate(key, tmp)
                expected = programs.parent / 'expected.json'
                expected.write_text(json.dumps({'results': [predict.model_result(p) for p in json.loads(programs.read_text())['programs']]}))
                run_slice1_vm.self_check(programs, expected)


if __name__ == '__main__': unittest.main()


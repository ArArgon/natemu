"""Accurate-FP plan chunk 0: the deviation registry, README's list of it, the judging rule, and each runner's no-VM self-check."""
import json, os, pathlib, re, sys, unittest
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools/reference'))
import fp_deviations as dev

RAW = pathlib.Path(os.environ.get('NATEMU_RAW_ROOT', ROOT.parent / 'natemu-raw'))


def readme_list():
    text = (ROOT / 'README.md').read_text()
    body = text.split('### Floating point on Macs without AFP (fast mode)\n', 1)[1].split('\n#', 1)[0]
    return dict(re.findall(r'^- `([a-z-]+)`: (.*)$', body, re.M))


class Registry(unittest.TestCase):
    def test_readme_lists_the_same_classes_with_the_same_descriptions(self):
        readme = readme_list()
        self.assertEqual(sorted(readme), sorted(dev.CLASSES))
        for name, (_, description, _) in dev.CLASSES.items():
            self.assertEqual(readme[name].rstrip('.'), description, name)

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
        import run_slice1_vm
        for n in ('01', '02'):
            d = RAW / f'i8-corpus-20261030-{n}'
            if not d.is_dir(): self.skipTest(f'no {d}')
            run_slice1_vm.self_check(d / 'programs.json', d / 'expected.json')


if __name__ == '__main__': unittest.main()


class I8bJudge(unittest.TestCase):
    """run_suite's i8b judge: on the M4 the 7 known QEMU departures fail; without AFP the tagged ones must be `deviated`."""
    def judge(self, rows):
        import tempfile, run_suite
        out = pathlib.Path(tempfile.mkdtemp()); (out / 'results.json').write_text(json.dumps({'results': rows}))
        return run_suite.i8b_judge(out)[0]
    def test_known_departures(self):
        import run_suite
        known, tagged = run_suite.I8B_KNOWN, run_suite.I8B_KNOWN & set(dev.corpus_deviations(run_suite.I8B_CORPUS))
        m4, m2 = {'afp': True, 'fp': 'fast'}, {'afp': False, 'fp': 'fast'}
        self.assertTrue(tagged)
        self.assertTrue(self.judge([{'name': n, 'outcome': 'fail', 'vm': m4} for n in known]))
        dev_rows = [{'name': n, 'outcome': 'deviated' if n in tagged else 'fail', 'vm': m2} for n in known]
        self.assertTrue(self.judge(dev_rows))
        self.assertFalse(self.judge([dict(r, outcome='fail') for r in dev_rows]))   # tagged ones exact or otherwise wrong
        self.assertFalse(self.judge([{'name': n, 'outcome': 'fail', 'vm': m4} for n in known] + [{'name': 'x', 'outcome': 'fail', 'vm': m4}]))

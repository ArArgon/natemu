"""CPython result parsing: subtest parameters are part of the case identifier."""
import importlib.util
from pathlib import Path
import tempfile
import unittest


spec = importlib.util.spec_from_file_location('cpy_check', Path(__file__).with_name('cpy-check.py'))
cpy_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpy_check)


class CaseParsing(unittest.TestCase):
    def test_spaced_subtests_keep_distinct_names_and_final_status(self):
        cases = {
            "test.test_example.Case.test_value (value='with spaces', mode=1)": 'skip',
            "test.test_example.Case.test_value (value='with spaces', mode=2)": 'fail',
            'test.test_example.Case.test_simple': 'error',
        }
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / 'serial'
            log.write_text(''.join(f'CT-CASE {name} {status}\n' for name, status in cases.items()))
            self.assertEqual(cpy_check.load(str(log))['cases'], cases)


spec = importlib.util.spec_from_file_location('cpy_junit', Path(__file__).with_name('cpy-junit.py'))
cpy_junit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpy_junit)

JUNIT = '''<testsuites>
<testsuite><testcase name="test.test_enum.T.test_a"/><testcase name="test.test_enum.T.test_b"/>
  <testcase name="enum.Enum"><failure/></testcase></testsuite>
<testsuite><testcase name="test.fork_wait.ForkWait.test_wait"/></testsuite>
<testsuite><testcase name="test.test_zipfile.Z.test_z"><skipped/></testcase></testsuite>
<testsuite><testcase name="test.test_asyncio.test_ssl.S.test_s"/></testsuite>
<testsuite><testcase name="mystery.case"/></testsuite>
<testsuite><testcase name="test.test_with.W.test_w"/></testsuite>
<testsuite><testcase name="another.mystery"/></testsuite>
<testsuite><testcase name="test.test_xx.X.test_x"/></testsuite>
</testsuites>'''
OUT = '''Run 8 tests in parallel using 4 worker processes
0:00:01 load avg: 0.00 [1/8] test_enum failed (1 failure)
0:00:02 load avg: 0.00 [2/8] test_fork1 passed
0:00:02 load avg: 0.00 [3/8] test_winreg skipped -- No module named 'winreg'
0:00:03 load avg: 0.00 [4/8] test_zipfile passed
0:00:03 load avg: 0.00 [5/8] test.test_asyncio.test_ssl passed
0:00:03 load avg: 0.00 [6/8] test_gc passed
0:00:03 load avg: 0.00 [7/8] test_glob passed
0:00:04 load avg: 0.00 [8/8] test_with passed
'''


class JunitAttribution(unittest.TestCase):
    def run_it(self, junit=JUNIT, listing='test_enum test_fork1 test_winreg test_zipfile test.test_asyncio.test_ssl '
               'test_gc test_glob test_with test_xx', out=OUT):
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            for n, text in (('j.xml', junit), ('list', listing), ('out', out)): (Path(d) / n).write_text(text)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf): cpy_junit.main(*(str(Path(d) / n) for n in ('j.xml', 'list', 'out')))
        return buf.getvalue().splitlines()

    def test_cases_go_to_their_module_or_their_testsuites(self):
        lines = self.run_it()
        res = {l.split()[2]: l for l in lines if l.startswith('CT-RESULT')}
        # enum.Enum (a doctest of the module tested) counts for test_enum; fork_wait's suite fills the one gap
        assert 'cases=3 pass=2 fail=1' in res['test_enum'] and 'CT-CASE enum.Enum fail' in lines
        assert 'cases=1 pass=1' in res['test_fork1']
        assert 'skip=1' in res['test_zipfile'] and 'cases=1' in res['test.test_asyncio.test_ssl']
        # between test_ssl and test_with two modules ran without an element: ambiguous, so not credited
        assert '?mystery' in res and 'test_gc' not in res and 'test_glob' not in res
        # after the last attributed element regrtest reported no other module: not credited either
        assert '?another' in res and 'cases=1' in res['test_xx']
        assert 'test_winreg' not in res

    def test_a_crashed_or_failed_module_gets_no_unnamed_suite(self):
        for said in ('crashed (Exit code -11)', 'failed (1 failure)'):
            lines = self.run_it(out=OUT.replace('[2/8] test_fork1 passed', f'[2/8] test_fork1 {said}'))
            res = {l.split()[2] for l in lines if l.startswith('CT-RESULT')}
            assert 'test_fork1' not in res and '?test' in res, said


if __name__ == '__main__':
    unittest.main()

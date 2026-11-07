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


if __name__ == '__main__':
    unittest.main()

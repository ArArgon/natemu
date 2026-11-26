"""The CF table's check: FexcCfTable (tools/fexc/shim/fexc_hook.h) against the std::map it replaced, on random sequences of
sets and lookups (tools/fexc/cf_table_check.cpp, built here with the README's clang++; it needs no fexc binary)."""
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class CfTable(unittest.TestCase):
    def test_answers_as_the_map(self):
        with tempfile.TemporaryDirectory(prefix='cf_table') as tmp:
            exe = Path(tmp) / 'cf_table_check'
            subprocess.run(['clang++', '-std=c++20', '-O1', '-I', str(HERE / 'shim'), '-o', str(exe), str(HERE / 'cf_table_check.cpp')],
                           check=True, capture_output=True)
            r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(r.stdout.startswith('ok '), r.stdout)


if __name__ == '__main__':
    unittest.main()

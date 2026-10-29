"""The resident's generated profile (crates/abi/src/cpu_profile.rs) is current with cpu_profile.json: regenerating it
gives the checked-in file. Its vectors are cpu_profile.py's answers, which the abi tests (crates/abi/src/cpu.rs) replay."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from models import gen_cpu_profile_rs as gen


class Generated(unittest.TestCase):
    def test_current(self):
        self.assertEqual(gen.OUT.read_text(), gen.render(), f'stale: run python3 {gen.__file__}')


if __name__ == '__main__':
    unittest.main()

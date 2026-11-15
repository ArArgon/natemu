"""The generated files are current with cpu_profile.json: regenerating gives the checked-in files, the resident's profile
(crates/abi/src/cpu_profile.rs; its vectors are cpu_profile.py's answers, which the abi tests (crates/abi/src/cpu.rs) replay)
and the gate constants (crates/abi/src/cpu_gates.rs, tools/fexc/shim/fexc_features.h)."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from models import gen_cpu_profile_rs as gen


class Generated(unittest.TestCase):
    def test_current(self):
        for out, render in gen.FILES:
            self.assertEqual(out.read_text(), render(), f'{out} is stale: run python3 {gen.__file__}')

    def test_header_values_are_the_states(self):
        p = gen.cp.load()
        text = gen.OUT_HEADER.read_text()
        for f in p.gated('shim'):
            self.assertIn(f'GF_{f["name"]} = {int(f["state"] == "advertised")};', text)
        self.assertEqual(text.count('inline constexpr int GF_'), len(p.gated('shim')))


if __name__ == '__main__':
    unittest.main()

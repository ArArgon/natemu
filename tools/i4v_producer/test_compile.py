import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import unittest

HERE=Path(__file__).parent
spec=importlib.util.spec_from_file_location('i4v_compile',HERE/'compile.py')
compiler=importlib.util.module_from_spec(spec);spec.loader.exec_module(compiler)

class CompileAdmission(unittest.TestCase):
    def args(self, *extra):
        # parse_args imports the same local case table as direct CLI invocation.
        sys.path.insert(0,str(HERE))
        try:
            return compiler.parse_args(['--directory','unused','--producer','unused','--rip','0x102000',*extra])
        finally: sys.path.pop(0)

    def test_case_selects_required_mode_without_launching_producer(self):
        low=self.args('--case','fex_moffs_load32')
        self.assertEqual((low.bitness,low.instructions,low.expected_host),(32,9,16))
        high=self.args('--case','fex_memory_data64')
        self.assertEqual((high.bitness,high.instructions,high.expected_host),(64,1,2))
        self.assertEqual(self.args('--source','90','--instructions','1').bitness,64)

    def test_conflicting_explicit_mode_rejects_before_compilation(self):
        for case,mode in [('fex_moffs_load32','64'),('fex_memory_data64','32')]:
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as caught:
                self.args('--case',case,'--bitness',mode)
            self.assertEqual(caught.exception.code,2)

if __name__=='__main__': unittest.main()

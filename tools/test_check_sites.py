"""Operand-width access-site validation, including a negative control for the former BT byte waiver."""
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / 'fexc'))
import check_sites as C


class BitTestSites(unittest.TestCase):
    def test_expected_operand_width(self):
        for name, width in [('word', 2), ('dword', 4), ('qword', 8)]:
            for mnemonic in ('bt', 'bts', 'btr', 'btc'):
                for offset in ('7', 'cx' if width == 2 else 'ecx' if width == 4 else 'rcx'):
                    line = f'{mnemonic} {name} ptr [rax], {offset}'
                    want = [('load', width)] + ([] if mnemonic == 'bt' else [('store', width)])
                    self.assertEqual(C.expected(line), want)
                    if mnemonic != 'bt': self.assertEqual(C.expected('lock ' + line), [('atomic', width)])

    @unittest.skipUnless(os.environ.get('FEXC'), 'requires the selected compiler in FEXC')
    def test_real_sites_and_narrowed_negative_control(self):
        original_run = C.run
        with tempfile.TemporaryDirectory() as directory, patch.object(C, 'FEXC', os.environ['FEXC']):
            for profile in ([], ['--profile', 'slice1']):
                with patch.object(C, 'PROFILE', profile):
                    for prefix, width, operand in [('66', 2, 'word'), ('', 4, 'dword'), ('48', 8, 'qword')]:
                        for mnemonic, encoded in [('bt', '0fba2007'), ('bts', '0fba2807'), ('btr', '0fba3007'), ('btc', '0fba3807')]:
                            for locked in (False, True) if mnemonic != 'bt' else (False,):
                                code = ('f0' if locked else '') + prefix + encoded + '90'
                                line = ('lock ' if locked else '') + f'{mnemonic} {operand} ptr [rax], 7'
                                program = {'name':line, 'code':code, 'rip':'0x100000', 'asm':[line, 'nop']}
                                problems, _ = C.check_program(program, pathlib.Path(directory))
                                self.assertEqual(problems, [], (profile, line, problems))
                                # Keep the actual generated code and object intact, but make metadata claim the old byte
                                # footprint. The EXPECTED-vs-SITES check itself must reject it, independent of other checks.
                                def narrowed(*args):
                                    result = original_run(*args)
                                    for site in result.get('sites', []):
                                        if site['kind'] not in C.STUBS: site['width'] = 1
                                    return result
                                with patch.object(C, 'run', side_effect=narrowed):
                                    problems, _ = C.check_program(program, pathlib.Path(directory))
                                self.assertTrue(any(kind == 'ERROR' and 'expected ' in msg and ' sites ' in msg
                                                    for kind, msg in problems), (profile, line, problems))


if __name__ == '__main__': unittest.main()

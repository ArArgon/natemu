"""census_report on synthetic census JSON (the plugin's format, tests/bench/plugins/indirect.c), and run_census's
reading of nasm's map file and its check of the plugin's counts."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import copy, unittest
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import census_report
import run_census


def ras(user, kernel):
    return {d: {'user': dict(zip(('hit', 'miss', 'empty'), user[i])), 'kernel': dict(zip(('hit', 'miss', 'empty'), kernel[i]))}
            for i, d in enumerate(('8', '16', '32'))}


def segment(name, sites, insns=(10000, 4000), direct=(30, 10), r=None, split=None):
    return {'name': name, 'vcpu_parts': 1, 'insns': {'user': insns[0], 'kernel': insns[1]},
            'direct_calls': {'user': direct[0], 'kernel': direct[1]},
            'far': {k: {'user': 0, 'kernel': 0} for k in ('call', 'jmp', 'ret')},
            'ras': r or ras([(80, 15, 5), (90, 10, 0), (95, 5, 0)], [(20, 0, 0), (20, 0, 0), (20, 0, 0)]),
            'ras_split': split or ras([(99, 1, 0), (100, 0, 0), (100, 0, 0)], [(20, 0, 0), (20, 0, 0), (20, 0, 0)]),
            'events': {'interrupts': 3, 'exceptions': 1, 'dropped': 0, 'unresolved': 0, 'wide': 0}, 'sites': sites}


# kind, mode, asid, pc, phys, flags, execs, overflow, targets
SITES = [
    ['call', 'user', 0x1000, 0x401000, 0x9000, 0, 60, 0, [[0x402000, 60]]],                   # one target
    ['jmp', 'user', 0x1000, 0x401100, 0x9100, 0, 30, 0, [[0x403000, 10], [0x403100, 20]]],    # two
    ['jmp', 'user', 0x1000, 0x401200, 0x9200, 1, 10, 2, [[1, 2], [2, 2], [3, 2], [4, 2]]],    # 5+ (overflow)
    ['call', 'user', 0x2000, 0x501000, 0x9000, 0, 40, 0, [[0x502000, 40]]],                   # same code, another process
                                                                                              # and load address
    ['call', 'kernel', 0, 0xffffffff81000000, 0x7000, 0, 50, 0, [[0xffffffff81001000, 50]]],
    ['ret', 'user', 0x1000, 0x402000, 0x9300, 0, 100, 0, [[0x401005, 100]]],
    ['ret', 'kernel', 0, 0xffffffff81001000, 0x7100, 0, 20, 0, [[0xffffffff81000005, 20]]],
]


def census(*segs):
    return {'plugin': 'indirect', 'version': 1, 'vcpus': 1, 'cr3': 'read', 'rip': 'read', 'segments': list(segs)}


class Reduce(unittest.TestCase):
    def setUp(self):
        self.r = census_report.reduce(census(segment('boot', []), segment('run1', SITES)), 'run1')

    def test_main_target_shares_are_execution_weighted(self):
        u = self.r['modes']['user']['indirect']
        self.assertEqual(u['execs'], 140)
        self.assertAlmostEqual(u['top1'], (60 + 20 + 2 + 40) / 140); self.assertAlmostEqual(u['top2'], (60 + 30 + 4 + 40) / 140)
        self.assertAlmostEqual(u['overflow'], 2 / 140)
        t = self.r['modes']['total']['indirect']
        self.assertEqual(t['execs'], 190); self.assertAlmostEqual(t['top1'], (122 + 50) / 190)
        self.assertEqual(self.r['modes']['kernel']['indirect']['top1'], 1.0)
        self.assertAlmostEqual(self.r['modes']['user']['indirect_jmp']['top1'], 22 / 40)

    def test_a_site_with_one_dominant_target_counts_by_its_share_not_its_class(self):
        r = census_report.reduce(census(segment('run1', [['jmp', 'user', 1, 0x10, 0, 0, 1000, 0, [[7, 999], [8, 1]]]])), 'run1')
        u = r['modes']['user']['indirect']
        self.assertAlmostEqual(u['top1'], 0.999); self.assertEqual(u['one_target_sites'], 0.0)

    def test_by_code_targets_relative_to_the_site_merge_load_addresses(self):
        u = self.r['modes']['user']['indirect_by_code']        # 0x401000 -> 0x402000 and 0x501000 -> 0x502000: one target
        self.assertEqual(u['sites'], 3)
        self.assertAlmostEqual(u['top1'], (100 + 20 + 2) / 140)
        self.assertEqual(u['sites_by_targets'], {'1': 1, '2': 1, '3': 0, '4': 0, '5+': 1})
        self.assertAlmostEqual(self.r['modes']['user']['indirect_call_by_code']['top1'], 1.0)

    def test_rets_return_model_and_rates_per_instruction(self):
        u, k, t = (self.r['modes'][m] for m in ('user', 'kernel', 'total'))
        self.assertEqual(u['ret']['execs'], 100); self.assertEqual(t['ret']['execs'], 120)
        self.assertAlmostEqual(u['ras']['8']['hit_rate'], 0.8); self.assertAlmostEqual(u['ras']['32']['hit_rate'], 0.95)
        self.assertAlmostEqual(t['ras']['8']['hit_rate'], 100 / 120)
        self.assertAlmostEqual(u['ras_split']['8']['hit_rate'], 0.99); self.assertAlmostEqual(t['ras_split']['8']['hit_rate'], 119 / 120)
        self.assertAlmostEqual(u['indirect_call']['one_target_sites'], 1.0); self.assertAlmostEqual(u['indirect_jmp']['two_target_sites'], 0.75)
        self.assertAlmostEqual(u['per_1000']['indirect'], 14.0); self.assertAlmostEqual(u['per_1000']['ret'], 10.0)
        self.assertAlmostEqual(k['per_1000']['direct_call'], 2.5); self.assertAlmostEqual(t['per_1000']['indirect'], 1000 * 190 / 14000)

    def test_a_missing_segment_is_an_error(self):
        with self.assertRaises(ValueError): census_report.reduce(census(segment('boot', [])), 'run1')

    def test_all_merges_segments_without_losing_the_target_classes(self):
        a = [['jmp', 'user', 1, 0x10, 0, 0, 5, 0, [[1, 5]]], ['jmp', 'user', 1, 0x20, 0, 0, 4, 0, [[1, 1], [2, 1], [3, 1], [4, 1]]]]
        b = [['jmp', 'user', 1, 0x10, 0, 0, 3, 0, [[2, 3]]], ['jmp', 'user', 1, 0x20, 0, 0, 1, 0, [[5, 1]]]]
        r = census_report.reduce(census(segment('x', a), segment('y', b)), 'all')
        u = r['modes']['user']
        self.assertEqual(u['indirect']['execs_by_targets'], {'1': 0, '2': 8, '3': 0, '4': 0, '5+': 5})
        self.assertEqual(u['insns'], 20000)
        self.assertEqual(r['modes']['total']['ras']['8']['hit'], 200)
        self.assertEqual(r['modes']['user']['ras_split']['16']['hit'], 200)

    def test_markdown_names_the_decision_rows(self):
        md = census_report.markdown(self.r, 'build')
        self.assertIn('| indirect executions to the main target / top two, per address space | 87.1% / 95.7% | 100.0% / 100.0% '
                      '| 90.5% / 96.8% |', md)
        self.assertIn('|   jmps | 55.0% / 85.0% |  | 55.0% / 85.0% |', md)
        self.assertIn('| secondary: indirect executions at sites that saw exactly one target | 71.4% | 100.0% | 78.9% |', md)
        self.assertIn('| return hit rate, one stack, depth 8 / 16 / 32 | 80.0% / 90.0% / 95.0% | 100.0% / 100.0% / 100.0% '
                      '| 83.3% / 91.7% / 95.8% |', md)
        self.assertIn('| return hit rate, user and kernel stacks apart, depth 8 / 16 / 32 | 99.0% / 100.0% / 100.0% |', md)


def rep_row(op, width, bits, mode, variant, **counts):
    c = dict.fromkeys(census_report.REP_COUNTS, 0); c.update(counts)
    return [op, width, bits, mode, variant, *(c[k] for k in census_report.REP_COUNTS)]


class Rep(unittest.TestCase):
    ROWS = [rep_row('movs', 1, 64, 'user', 0, instances=10, bytes=4000, inst_1_16_rounds=6, inst_more=4, bytes_eligible=3000,
                    bytes_bulk=2800, bytes_aligned8=1000, bytes_overlap=1000),
            rep_row('movs', 8, 64, 'kernel', 0, instances=2, bytes=1000, inst_more=2, bytes_aligned8=1000, bytes_eligible=1000,
                    bytes_bulk=990),
            rep_row('movs', 8, 64, 'user', 0, instances=1, bytes=1000, inst_more=1, bytes_aligned4=1000),     # not 8-aligned
            rep_row('movs', 1, 64, 'user', 1, instances=1, bytes=5, inst_under_round=1),                      # 0x67: not in the cut
            rep_row('movs', 4, 32, 'user', 0, instances=4, bytes=400, inst_1_16_rounds=4, bytes_eligible=200, bytes_bulk=200),
            rep_row('stos', 8, 64, 'kernel', 0, instances=1, bytes=4096, inst_more=1, bytes_eligible=4096, bytes_bulk=4096)]

    def test_the_first_cut_shares_count_64_bit_movsb_and_movsq_bytes_only(self):
        seg = dict(segment('run1', []), rep=self.ROWS, rep_continued=3)
        r = census_report.reduce(census(seg), 'run1')['rep']
        g = r['groups']['first_cut']
        self.assertEqual(g['total']['bytes'], 4000 + 1000 + 1000 + 5)          # the 0x67 form is a 64-bit MOVSB too
        self.assertAlmostEqual(g['total']['eligible_share'], 4000 / 6005)
        self.assertAlmostEqual(g['total']['bulk_share'], 3790 / 6005)
        self.assertAlmostEqual(g['user']['bulk_share'], 2800 / 5005); self.assertAlmostEqual(g['kernel']['bulk_share'], 0.99)
        self.assertAlmostEqual(r['groups']['movsd32']['total']['eligible_share'], 0.5)
        self.assertEqual(r['forms'][0]['form'], 'rep stosq 64-bit kernel')
        self.assertIn('rep movsb 64-bit user a32', [f['form'] for f in r['forms']])
        self.assertEqual(r['continued'], 3)
        md = census_report.markdown(census_report.reduce(census(seg), 'run1'), 'zstd')
        self.assertIn('| 64-bit MOVSB + MOVSQ: moved in bulk | 55.9% | 99.0% | 63.1% |', md)
        self.assertIn('| rep movsb 64-bit user | 10 | 0.00 | 0 / 0 / 60 / 40 | 25.0% | 25.0% | 0.0% | 75.0% | 70.0% |', md)

    def test_all_sums_the_forms_of_every_segment(self):
        a = dict(segment('x', []), rep=self.ROWS[:1], rep_continued=1)
        b = dict(segment('y', []), rep=self.ROWS[:2], rep_continued=2)
        r = census_report.reduce(census(a, b), 'all')['rep']
        self.assertEqual(r['groups']['first_cut']['user']['bytes'], 8000); self.assertEqual(r['continued'], 3)

    def test_no_rep_counts_say_so(self):
        md = census_report.markdown(census_report.reduce(census(segment('run1', [])), 'run1'), 'x')
        self.assertIn('No REP MOVS or STOS counted', md)


class Check(unittest.TestCase):
    MAP = """
-- Symbols --------------------------------------------------------------------

---- No Section ---------------------------------------------------------------

Value     Name
FFFFFFFF80000000  KBASE
00400000  ALIAS

---- Section .text ------------------------------------------------------------

Real              Virtual           Name
          100000            100000  mb_header
          10005E            10005E  long_entry
"""

    def test_symbols_reads_constants_and_labels(self):
        self.assertEqual(run_census.symbols(self.MAP), {'KBASE': 0xffffffff80000000, 'ALIAS': 0x400000,
                                                       'mb_header': 0x100000, 'long_entry': 0x10005e})

    def synthetic(self):
        """The plugin's output the check expects, made from EXPECTED_* and made-up addresses."""
        names = {lab for _, _, lab, _, tg, *_ in run_census.EXPECTED_SITES for lab in [lab, *(t for t, _ in tg)]}
        sym = {n: 0x100000 + 0x10 * i for i, n in enumerate(sorted(names))}
        sym.update(KBASE=0xffffffff80000000, ALIAS=0x400000, HALIAS=0x600000, pml4=0x103000, pml4b=0x108000,
                   mb_header=0x100000, image_end=0x109000)
        a = lambda lab, al: sym[lab] + (sym[al] if al else 0)
        sites = [[k, m, (sym[sp[0]] if sp else 0x103000) & ~0x1fff if m == 'user' else 0, a(lab, al), 0, fl,
                  sum(tg.values()) + ov, ov, [[a(t, tal), c] for (t, tal), c in tg.items()]]
                 for k, m, lab, al, tg, ov, fl, *sp in run_census.EXPECTED_SITES]
        seg = {'name': 'program', 'sites': sites, 'ras': copy.deepcopy(run_census.EXPECTED_RAS),
               'ras_split': copy.deepcopy(run_census.EXPECTED_RAS_SPLIT),
               'insns': {'user': 9000, 'kernel': 40}, **copy.deepcopy(run_census.EXPECTED_COUNTS),
               'rep': [[*k, *v] for k, v in run_census.EXPECTED_REP.items()], 'rep_continued': run_census.EXPECTED_REP_CONTINUED, 'rep_df_unknown': 0}
        return {'cr3': 'read', 'rip': 'read', 'rep': 'read', 'segments': [{'name': 'boot', 'sites': []}, seg]}, sym

    def test_the_expected_counts_pass(self):
        data, sym = self.synthetic()
        self.assertEqual(run_census.check_problems(data, sym), [])

    def test_each_kind_of_difference_fails(self):
        data, sym = self.synthetic()
        seg = data['segments'][1]
        bad = copy.deepcopy(data); bad['segments'][1]['sites'][4][6] += 1            # one execution more
        self.assertTrue(any('A_call' in p for p in run_census.check_problems(bad, sym)))
        bad = copy.deepcopy(data); del bad['segments'][1]['sites'][9]                 # C_jmp missing
        self.assertTrue(any('C_jmp' in p and 'no site' in p for p in run_census.check_problems(bad, sym)))
        bad = copy.deepcopy(data); bad['segments'][1]['sites'].append(['ret', 'user', 0, 0x100002, 0, 0, 1, 0, []])
        self.assertTrue(any('unexpected site' in p for p in run_census.check_problems(bad, sym)))
        bad = copy.deepcopy(data); bad['segments'][1]['ras']['8']['user']['empty'] = 0
        self.assertTrue(any('return model' in p for p in run_census.check_problems(bad, sym)))
        bad = copy.deepcopy(data); bad['segments'][1]['ras_split']['16']['kernel']['hit'] = 6
        self.assertTrue(any('split return model' in p for p in run_census.check_problems(bad, sym)))
        bad = copy.deepcopy(data); bad['segments'][1]['events']['dropped'] = 0
        self.assertTrue(any('events' in p for p in run_census.check_problems(bad, sym)))
        bad = copy.deepcopy(data); bad['segments'][1]['sites'][13][5] = 0               # E2's REX flag lost
        self.assertTrue(any('E2' in p for p in run_census.check_problems(bad, sym)))
        bad = copy.deepcopy(data); bad['segments'][1]['sites'][4][2] = 0                # user site without its CR3
        self.assertTrue(any('A_call' in p and 'only in address spaces' in p for p in run_census.check_problems(bad, sym)))
        bad = copy.deepcopy(data)                                                       # one address space for J
        for s in bad['segments'][1]['sites']:
            if s[2] == 0x108000: s[2] = 0x102000
        self.assertTrue(any('X_call' in p for p in run_census.check_problems(bad, sym)))
        bad = copy.deepcopy(data); bad['segments'][1]['rep'][0][5] = 5                  # S1's 3 instances counted as 2
        self.assertTrue(any("REP ('movs', 1, 64, 'user', 0)" in p for p in run_census.check_problems(bad, sym)))
        bad = copy.deepcopy(data); bad['segments'][1]['rep'][1][-1] = 560000            # S2's bulk bytes
        self.assertTrue(any("REP ('movs', 8, 64" in p for p in run_census.check_problems(bad, sym)))
        bad = copy.deepcopy(data); bad['segments'][1]['rep'][4][2] = 64                 # S9 counted as 64-bit code
        self.assertTrue(any('REP' in p for p in run_census.check_problems(bad, sym)))
        bad = copy.deepcopy(data); bad['segments'][1]['rep_continued'] = 0
        self.assertTrue(any('REP continuations' in p for p in run_census.check_problems(bad, sym)))
        bad = copy.deepcopy(data); bad['segments'][1]['rep_df_unknown'] = 1
        self.assertTrue(any('REP directions' in p for p in run_census.check_problems(bad, sym)))
        bad = copy.deepcopy(data); bad['rep'] = 'unavailable'
        self.assertTrue(any('REP registers' in p for p in run_census.check_problems(bad, sym)))
        bad = copy.deepcopy(data); bad['segments'][1]['name'] = 'boot2'
        self.assertTrue(any('no segment' in p for p in run_census.check_problems(bad, sym)))
        self.assertEqual(seg['name'], 'program')


if __name__ == '__main__':
    unittest.main()

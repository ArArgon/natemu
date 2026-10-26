import copy
import unittest

import check_i4v_fence as oracle


def report_fixture(profile=0, n=4):
    """Synthetic model report; never evidence that native execution happened."""
    vm = 0x123456789
    record = dict(case='fence', profile=profile, n=n, configured_vcpus=n, tested_vcpus=n,
                  trial_count=16, vm_nonce=vm, owners=[],
                  images=[bytes(oracle.expected_image(profile, n, t)).hex() for t in range(16)],
                  source_before=oracle.source_image(n), source_after=oracle.source_image(n),
                  tables_before=oracle.initial_tables(), tables_after=oracle.initial_tables(),
                  common=dict(compiled=(1 << n) - 1, stopped=(1 << n) - 1, epoch=16,
                              ready=[16] * n + [0] * (4 - n), done=[16] * n + [0] * (4 - n)))
    for cpu in range(n):
        requests = [oracle.packet(vm, profile, n, cpu, c) for c in range(4)]
        replies = [oracle.reply(vm, profile, n, cpu, c) for c in range(4)]
        sink, returned = oracle.PRIVATE + 0xa0080, oracle.PRIVATE + 0xa0100
        code = oracle.code_image(sink, returned)
        bank = [((i * 37 + 19) & 255) ^ cpu for i in range(512)]
        cell = dict(id=cpu, trial=16, completed=16, expected=requests[-1], requests=requests,
                    replies=replies, transfers=4, walks=3, table_reads=12, table_writes=0,
                    routes=[oracle.DATA, oracle.RESULTS, oracle.RIP + cpu * 256], code=code, code_length=128,
                    sink_pc=sink, return_pc=returned, status=1, detail=0, current_el=4, sctlr=0x1005,
                    actlr=2, tcr=oracle.TCR, capture_pc=returned, service_pc=returned + 4,
                    initial=oracle.owner_initial(cpu), bank_before=bank, bank_after=bank,
                    published_code=code, final_native_leaf=oracle.native_leaf(), compiler_requests=requests,
                    trials=[])
        for trial in range(16):
            case = oracle.selected(profile, cpu, trial)
            initial, prefix, final, _, mxcsr = oracle.results(cpu, case)
            cell['trials'].append(dict(epoch=trial + 1, case=case, completed=1, fence_completed=1,
                                       sink_count=1, sink_output=[0xa5] * 40, mxcsr_before=oracle.mxcsr_before(case),
                                       mxcsr=mxcsr, actlr=2, continuation=oracle.CODE + 24,
                                       initial=initial, sink=prefix, captured=final))
        cell['current'] = copy.deepcopy(cell['trials'][-1]['captured'])
        cell['captured'] = copy.deepcopy(cell['current'])
        record['owners'].append(cell)
    return record


class FenceOracleTests(unittest.TestCase):
    def reject(self, report):
        with self.assertRaises((AssertionError, ValueError, TypeError, KeyError)):
            oracle.audit_development(report)

    def test_literal_arithmetic_and_fp_results(self):
        # Independently fixed architectural cases anchor the report generator.
        expected = [
            (0, 0x5700, 0x60000000, 0x40000000, 0, 0x40000000, 0x1fa0),
            (0x8000000000000000, 0x9601, 0x90000000, 0xb0000000, 0, 0x80000000, 0x1fa0),
            (16, 0x1200, 0, 0x20000000, 0x15, 0, 0x1fa1),
            (3, 0x0600, 0, 0x20000000, 3, 0, 0x1fb0),
            (3, 0x0600, 0, 0x20000000, 3, 0, 0x1f80),
        ]
        for case, row in enumerate(expected):
            initial, sink, final, output, mxcsr = oracle.results(0, case)
            self.assertEqual((final['x'][7], final['x'][4] & 0xffff, sink['nzcv'], final['nzcv'],
                              final['x'][0], final['x'][21], mxcsr), row)
            self.assertEqual(final['x'][4] >> 16, initial['x'][4] >> 16)
            self.assertEqual(sink['q'][16], oracle.vector_bytes([0x3f800001 if case == 4 else 0x3f800000,
                                                               0x40800000, 0x40800000, 0x40a00000]))
            self.assertEqual(sink['fpsr'], 0 if case == 4 else 16)
            self.assertEqual(final['x'][20], 16)
            self.assertEqual(output[10:16] + output[36:40], [0xa5] * 10)

    def test_binary32_rounding_ties_even(self):
        self.assertEqual(oracle.fp_add_positive_normal(0x3f800000, 0x33800000), (0x3f800000, True))
        self.assertEqual(oracle.fp_add_positive_normal(0x3f800001, 0x33800000), (0x3f800002, True))
        self.assertEqual(oracle.fp_add_positive_normal(0x3f800000, 0x34000000), (0x3f800001, False))
        self.assertEqual(oracle.fp_add_positive_normal(0x40000000, 0x40000000), (0x40800000, False))

    def test_all_twenty_configurations(self):
        for profile in range(5):
            for n in range(1, 5):
                result = oracle.audit_development(report_fixture(profile, n))
                self.assertEqual(result['completion_sinks'], n * 16)
                self.assertEqual(result['transfers'], n * 4)

    def test_prefix_is_live_before_suffix(self):
        for field, index in [('x', 7), ('x', 26), ('x', 27), ('x', 16), ('x', 30), ('x', 20)]:
            record = report_fixture()
            record['owners'][0]['trials'][0]['sink'][field][index] ^= 1
            self.reject(record)
        for key in ('nzcv', 'fpsr', 'fpcr'):
            record = report_fixture()
            record['owners'][0]['trials'][0]['sink'][key] ^= 1
            self.reject(record)
        record = report_fixture()
        record['owners'][0]['trials'][0]['sink_output'][0] = 0
        self.reject(record)

    def test_generated_suffix_and_all_preserved_roots(self):
        for index in (0, 4, 5, 7, 16, 20, 21, 26, 27, 28, 30):
            record = report_fixture()
            record['owners'][1]['trials'][0]['captured']['x'][index] ^= 1
            self.reject(record)
        for index in (0, 16, 17, 31):
            record = report_fixture()
            record['owners'][0]['trials'][0]['captured']['q'][index][0] ^= 1
            self.reject(record)
        record = report_fixture()
        t = record['owners'][0]['trials'][0]
        t['captured']['nzcv'] = t['sink']['nzcv']
        self.reject(record)

    def test_sticky_status_cannot_be_replaced_by_current_exception(self):
        for case in (2, 3):
            record = report_fixture(case)
            record['owners'][0]['trials'][0]['mxcsr'] = 0x1fa0
            self.reject(record)
        record = report_fixture(4)
        record['owners'][0]['trials'][0]['mxcsr'] |= 0x20
        self.reject(record)

    def test_every_ram_byte_and_tables_source_are_bound(self):
        for at in (0, 4096, 4104, 4106, 4112, 4128, 4132, 8191):
            record = report_fixture()
            image = bytearray.fromhex(record['images'][0]); image[at] ^= 1
            record['images'][0] = image.hex()
            self.reject(record)
        for field, at in (('tables_after', 0), ('source_after', 25), ('source_before', 4095)):
            record = report_fixture(); record[field][at] ^= 1
            self.reject(record)

    def test_packet_identity_and_exact_image(self):
        self.assertEqual(len(oracle.BODY), 120)
        self.assertEqual(len(oracle.SOURCE), 25)
        for chunk in range(4):
            for word in range(16):
                record = report_fixture(n=1)
                record['owners'][0]['replies'][chunk][word] ^= 1
                self.reject(record)
        for at in (16, 20, 24, 120, 124):
            record = report_fixture()
            record['owners'][0]['published_code'][at] ^= 1
            self.reject(record)

    def test_gates_counters_and_transport_are_not_execution(self):
        for field in ('sink_count', 'fence_completed', 'completed', 'continuation', 'epoch', 'actlr'):
            record = report_fixture()
            record['owners'][0]['trials'][0][field] = 0
            self.reject(record)
        for field in ('walks', 'table_reads', 'table_writes', 'transfers'):
            record = report_fixture(); record['owners'][0][field] += 1
            self.reject(record)
        record = report_fixture(); record['common']['done'][0] -= 1
        self.reject(record)
        record = report_fixture(); record['owners'][0]['trials'][0]['completed'] = True
        self.reject(record)
        record = report_fixture(); record['unexpected'] = 0
        self.reject(record)

    def test_requested_identity_is_bound(self):
        self.assertEqual(oracle.check(report_fixture(), 0, 4)['native_entries'], 64)
        for profile, n in ((1, 4), (0, 3), (True, 4), (0, False), (-1, 4), (0, '4')):
            with self.assertRaises((AssertionError, ValueError, TypeError)):
                oracle.check(report_fixture(), profile, n)

    def test_malformed_identity_and_transport_fail_closed(self):
        for record in (None, [], {}, {'profile': 0}, {'profile': True, 'n': 1},
                       {'profile': 0, 'n': 0}, {'profile': 5, 'n': 1}):
            with self.assertRaises((AssertionError, ValueError, TypeError)):
                oracle.check(record)
        for key, value in (('owners', []), ('owners', [0]), ('images', []), ('vm_nonce', 0)):
            record = report_fixture(); record[key] = value
            with self.assertRaises((AssertionError, ValueError, TypeError, KeyError)):
                oracle.check(record)


if __name__ == '__main__':
    unittest.main()

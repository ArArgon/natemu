import struct
import tempfile
import unittest

import i4v_address_worker as worker


class AddressWorkerTests(unittest.TestCase):
    def test_compiler_slots_bound_active_work_and_release_on_failure(self):
        with tempfile.TemporaryDirectory() as root:
            with worker.compiler_slot(root, timeout=0):
                with worker.compiler_slot(root, timeout=0):
                    with self.assertRaises(TimeoutError):
                        with worker.compiler_slot(root, timeout=0):
                            self.fail('third active compiler admitted')
                with self.assertRaisesRegex(RuntimeError, 'compiler failure'):
                    with worker.compiler_slot(root, timeout=0):
                        raise RuntimeError('compiler failure')
                with worker.compiler_slot(root, timeout=0):
                    pass
            with worker.compiler_slot(root, timeout=0):
                with worker.compiler_slot(root, timeout=0):
                    pass

    def test_owner_mode_fetch_address_and_source_are_bound_separately(self):
        for n in range(1, 5):
            for actor in range(n):
                for owner in range(n):
                    for profile in range(6):
                        for generation in (1, 2):
                            for step in range(3):
                                p = worker.request(37, n, profile, actor, owner, generation, step + 1, 0, step)
                                self.assertEqual(worker.parse_request(struct.pack('<16Q', *p)), p)
                                spec = worker.CASES[worker.case_for(profile, generation, step)]
                                r = struct.unpack('<16Q', worker.make_reply(p, bytes.fromhex(spec['body'])))
                                self.assertEqual(r[:9], (worker.DELIVER,) + p[1:9])
                                self.assertEqual(r[11:], p[11:])
                                self.assertEqual(p[11], (0x300123 if owner == actor else 0x400123) + p[7])
                                self.assertEqual(p[12], (0x200123 if profile == 1 else 0x300123) + p[7])

    def test_malformed_bindings_reject_before_compilation(self):
        p = worker.request(37, 4, 3, 2, 1, 2, 5, 0, 1)
        for field in (0, 1, 4, 6, 7, 8, 9, 10, 11, 12, 14, 15):
            q = list(p)
            q[field] ^= 1
            with self.subTest(field=field), self.assertRaises(ValueError):
                worker.parse_request(struct.pack('<16Q', *q))
        for context_bit in (8, 18, 25, 26, 63):
            q = list(p)
            q[6] ^= 1 << context_bit
            with self.subTest(bit=context_bit), self.assertRaises(ValueError):
                worker.parse_request(struct.pack('<16Q', *q))
        for field, value in ((2, 0), (3, 4), (5, 0), (13, 0)):
            q = list(p)
            q[field] = value
            with self.assertRaises(ValueError):
                worker.parse_request(struct.pack('<16Q', *q))
        with self.assertRaises(ValueError):
            worker.make_reply(p, b'\0' * 16)

    def test_terminal_cs_checks_whole_instruction_only_for_actor(self):
        p = worker.request(37, 4, 5, 2, 2, 2, 9, 1, 0)
        self.assertEqual(p[7], 0x2000)
        for step in (1, 2):
            with self.assertRaises(ValueError):
                worker.request(37, 4, 5, 2, 2, 2, 9, 1, step)
            with self.assertRaises(ValueError):
                worker.request(37, 4, 5, 2, 1, 2, 9, 1, step)
            self.assertEqual(worker.request(37, 4, 5, 2, 1, 2, 9, 0, step)[3], 1)


if __name__ == '__main__':
    unittest.main()

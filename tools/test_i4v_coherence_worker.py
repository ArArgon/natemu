import struct
import unittest
from unittest.mock import patch

import i4v_coherence_worker as worker


class CoherenceWorkerTests(unittest.TestCase):
    def test_full_matrix_binds_decode_mode_and_each_owner(self):
        cells = 0
        for profile in range(6):
            for n in range(1, 5):
                for actor in range(n):
                    cells += 1
                    for owner in range(n):
                        for kind in range(2):
                            p = worker.request(37, n, profile, actor, owner, kind)
                            self.assertEqual(worker.parse_request(struct.pack('<16Q', *p)), p)
                            self.assertEqual(p[8], 64 if profile in (0, 3, 5) else 32)
                            body = bytes.fromhex(worker.CASES[worker.case_of(p)]['body'])
                            reply = struct.unpack('<16Q', worker.make_reply(p, body))
                            self.assertEqual(reply[1:14], p[1:14])
                            self.assertEqual(reply[14].to_bytes(8, 'little'), body)
                            for at in range(16):
                                bad = list(p); bad[at] ^= 1
                                try:
                                    parsed = worker.parse_request(struct.pack('<16Q', *bad))
                                except ValueError:
                                    continue
                                self.assertNotEqual(worker.make_reply(parsed, body), worker.make_reply(p, body))
        self.assertEqual(cells, 60)

    def test_wrong_decoding_and_source_fail_before_any_compile(self):
        p = worker.request(37, 4, 0, 2, 3, 0)
        with patch.object(worker, 'compile_source') as compile_source:
            for at, value in ((8, 32), (9, 0x038b), (12, 1), (6, p[6] | (1 << 40))):
                bad = list(p); bad[at] = value
                with self.assertRaises(ValueError):
                    worker.parse_request(struct.pack('<16Q', *bad))
            compile_source.assert_not_called()
        with self.assertRaises(ValueError):
            worker.make_reply(p, bytes(8))
        for vm in (0, 1 << 64, True):
            with self.assertRaises(ValueError):
                worker.request(vm, 1, 0, 0, 0, 0)


if __name__ == '__main__':
    unittest.main()

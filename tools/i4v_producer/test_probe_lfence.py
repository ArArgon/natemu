import unittest

try:
    from .probe_lfence import INSTRUCTIONS, SOURCE, fence_offset
except ImportError:
    from probe_lfence import INSTRUCTIONS, SOURCE, fence_offset


class ProbeTests(unittest.TestCase):
    def test_source_boundary_excludes_semantic_status_store(self):
        self.assertEqual(len(SOURCE), 25)
        self.assertEqual(len(INSTRUCTIONS), 8)
        self.assertEqual([at for at, _, _ in INSTRUCTIONS], [0, 3, 6, 9, 10, 13, 16, 20])
        self.assertEqual(sum(size for _, size, _ in INSTRUCTIONS), len(SOURCE))
        self.assertEqual(SOURCE[6:13], bytes.fromhex('0faee89f0f90c0'))

    def test_fence_requires_one_aligned_load_barrier(self):
        fence = bytes.fromhex('bf3d03d5')
        nop = bytes.fromhex('1f2003d5')
        self.assertEqual(fence_offset(nop + fence + nop), 4)
        for body in (b'', nop, fence + fence, fence + b'\0',
                     bytes.fromhex('bf3f03d5'), b'\0' + fence + b'\0\0\0'):
            with self.assertRaises(ValueError):
                fence_offset(body)


if __name__ == '__main__':
    unittest.main()

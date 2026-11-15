"""tools/boot_files.py: GPT entries give each partition's byte offset and mixed-endian PARTUUID; empty entries are skipped."""
import struct
import unittest
import uuid

import boot_files


class Partitions(unittest.TestCase):
    def test_entries(self):
        head = bytearray(34 * 512)
        head[512:520] = b'EFI PART'
        struct.pack_into('<QII', head, 512 + 72, 2, 128, 128)
        part = uuid.UUID('19943023-2413-48ef-ae3d-c0972ee1b031')
        entry = 2 * 512 + 128          # the second entry; the first stays empty
        head[entry:entry + 16] = b'\x01' * 16
        head[entry + 16:entry + 32] = part.bytes_le
        struct.pack_into('<Q', head, entry + 32, 262144)
        self.assertEqual(boot_files.partitions(bytes(head)), [(262144 * 512, str(part))])

    def test_no_gpt(self):
        with self.assertRaises(ValueError):
            boot_files.partitions(bytes(34 * 512))


if __name__ == '__main__':
    unittest.main()

"""Verify upstream vectors, instruction prefixes, relocation and provenance."""

import hashlib
import json
from pathlib import Path
import unittest

from expectations import COUNTS, expected_after

ROOT = Path(__file__).resolve().parent


class FixtureTests(unittest.TestCase):
    def test_provenance_and_encodings(self):
        manifest = json.loads((ROOT / "cases.json").read_text())
        for case in manifest["cases"]:
            with self.subTest(case=case["id"]):
                self.assertEqual(hashlib.sha256((ROOT / case["upstream_copy"]).read_bytes()).hexdigest(), case["upstream_sha256"])
                source = bytes.fromhex(case["x86_hex"])
                self.assertEqual(hashlib.sha256(source).hexdigest(), case["x86_sha256"])
                self.assertEqual(len(case["instruction_offsets"]), COUNTS[case["id"]])
                self.assertEqual(case["instruction_offsets"], sorted(set(case["instruction_offsets"])))
                self.assertLess(case["instruction_offsets"][-1], len(source))
                original = (ROOT / case["upstream_copy"]).read_text()
                config = json.loads(original.split("%ifdef CONFIG", 1)[1].split("%endif", 1)[0])
                self.assertEqual({k: int(v, 16) for k, v in case["original_expected_registers"].items()},
                                 {k: int(v, 16) for k, v in config["RegData"].items()})
        license_info = manifest["upstream"]
        self.assertEqual(hashlib.sha256((ROOT / license_info["license_path"]).read_bytes()).hexdigest(), license_info["license_sha256"])

    def test_load_at_low_high_and_page_boundaries(self):
        for base in (0x100000, 0x100FF9, 0x7FFFFFFFFFF8, 0xFFFF800000000000, 0xFFFFFFFFFFFFF000):
            with self.subTest(base=hex(base)):
                mem = dict(enumerate(bytes.fromhex("aabbccdd00000000"), base))
                regs = {"RAX": 123, "RBX": base, "RFLAGS": 0xAD7}
                actual, after = expected_after("fex_memory_data64", regs, mem)
                self.assertEqual(actual, {**regs, "RAX": 0xDDCCBBAA})
                self.assertEqual(after, mem)
                self.assertEqual(expected_after("fex_memory_data64", regs, mem, 0), (regs, mem))
                self.assertEqual(regs["RAX"], 123)

    def test_stack_original_vector_and_relocated_prefixes(self):
        for top in (0xE0000020, 0x101006, 0xFFFF800000101006):
            with self.subTest(top=hex(top)):
                mem = dict(enumerate(bytes([0xA5]) * 24, top - 16))
                regs = {"RAX": 0xA0, "RBX": 0xB0, "RSP": top, "RDX": top, "RFLAGS": 0xAD7}
                for completed, delta in enumerate((0, 8, 10, 12, 12, 12)):
                    actual, after = expected_after("fex_push_immediates64", regs, mem, completed)
                    self.assertEqual(actual["RSP"], top - delta)
                    self.assertEqual(actual["RFLAGS"], regs["RFLAGS"])
                    self.assertEqual(actual["RDX"], top)
                    self.assertEqual(actual["RAX"], 0xFFFFFFFFBEBDBCBB if completed >= 4 else 0xA0)
                    self.assertEqual(actual["RBX"], 0x51526162 if completed >= 5 else 0xB0)
                    for addr in range(top - 16, top - delta):
                        self.assertEqual(after[addr], 0xA5)
                    for addr in range(top, top + 8):
                        self.assertEqual(after[addr], 0xA5)
                self.assertEqual(bytes(after[addr] for addr in range(top - 12, top)).hex(), "62615251bbbcbdbeffffffff")
                self.assertTrue(all(v == 0xA5 for v in mem.values()))

    def test_32bit_upstream_vectors_and_partial_memory(self):
        for case, eax, ecx, final_byte in (("fex_moffs_load32", 0xFFFF0042, 0xFFFF0042, 0x42), ("fex_moffs_store32", 0xFFFF0043, 0x43, 0x43)):
            with self.subTest(case=case):
                regs = {"RAX": 0xDEADBEEF, "RCX": 0xABCD, "RDX": 0, "RFLAGS": 0xAD7}
                mem = dict(enumerate(bytes([0xA5]) * 8, 0x100000))
                actual, after = expected_after(case, regs, mem)
                self.assertEqual(actual, {**regs, "RAX": eax, "RCX": ecx, "RDX": 0x42})
                self.assertEqual(bytes(after[addr] for addr in range(0x100000, 0x100008)), bytes([final_byte, 0, 0, 0, 0xA5, 0xA5, 0xA5, 0xA5]))
                for prefix in range(COUNTS[case] + 1):
                    partial, _ = expected_after(case, regs, mem, prefix)
                    self.assertEqual(partial["RFLAGS"], regs["RFLAGS"])

    def test_missing_bytes_and_invalid_prefix_fail(self):
        with self.assertRaises(KeyError):
            expected_after("fex_memory_data64", {"RBX": 0x100000}, {})
        with self.assertRaises(ValueError):
            expected_after("fex_memory_data64", {}, {}, 2)


if __name__ == "__main__":
    unittest.main()

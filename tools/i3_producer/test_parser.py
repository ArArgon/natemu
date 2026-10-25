import hashlib
import json
from pathlib import Path
import re
import struct
import unittest

try:
    from .parser import ALIAS, FP_ALIAS, ROOTS, make_input, parse
except ImportError:
    from parser import ALIAS, FP_ALIAS, ROOTS, make_input, parse


DATA = Path(__file__).with_name("testdata")


class ParserTests(unittest.TestCase):
    def load(self, label="alias"):
        return ((DATA / (label + ".txt")).read_text(),
                (DATA / (label + ".native")).read_bytes(),
                (DATA / (label + ".x86")).read_bytes())

    def test_retained_source_identity(self):
        provenance = json.loads((DATA / "provenance.json").read_text())
        producer = json.loads(DATA.parent.joinpath("producer.json").read_text())
        self.assertEqual(provenance["producer_binary_sha256"], producer["binary_sha256"])
        for name, digest in provenance["files"].items():
            self.assertEqual(hashlib.sha256((DATA / name).read_bytes()).hexdigest(), digest)

    def test_fresh_single_request_maps(self):
        for label in ("alias", "fp_alias"):
            with self.subTest(label=label):
                log, raw, source = self.load(label)
                result = parse(log, raw, 1, source)
                self.assertEqual(result["count_tool_status"], 0)
                self.assertEqual(result["body"], raw[12:320])
                self.assertEqual(result["spill_bytes"], 768)
                self.assertEqual([s["native_offset"] for s in result["sites"]], [148, 296])
                for site in result["sites"]:
                    self.assertEqual([r["role"] for r in site["roots"]], ROOTS)
                    self.assertEqual(sum(r["kind"] == "spill" for r in site["roots"]), 12)
                    self.assertFalse(site["carry_inverted"])
                    self.assertEqual(site["native_fp_prefix"], label == "fp_alias")
                # Two independently allocated values: old address, then loaded RAX.
                self.assertEqual(result["sites"][0]["roots"][0]["offset"], 0)
                self.assertEqual(result["sites"][1]["roots"][0]["offset"], 384)

    def test_single_input_encoding(self):
        for source in (ALIAS, FP_ALIAS):
            encoded = make_input(source)
            self.assertEqual(struct.unpack_from("<6Q", encoded), (64, 25, 1, 8192, 49155, 0))
            label, expected_count, size, count, cookie = struct.unpack_from("<128sqQQI", encoded, 48)
            self.assertEqual((label.rstrip(b"\0"), expected_count, size, count, cookie),
                             (b"request", 77, len(source), 5, 0x41424344))
            self.assertEqual(encoded[204:], source)
        for source in (b"", ALIAS[:-1], b"\x90"):
            with self.assertRaises(ValueError):
                make_input(source)

    def test_corrupt_metadata_rejected(self):
        log, raw, source = self.load()
        first_root = next(line for line in log.splitlines() if "role=RAX " in line)
        spill = next(line for line in log.splitlines() if line.startswith("NATEMU_SPILL "))
        feature = next(line for line in log.splitlines() if line.startswith("NATEMU_FEATURES "))
        changes = {
            "missing root": log.replace(first_root + "\n", "", 1),
            "duplicate root": log.replace(first_root, first_root + "\n" + first_root, 1),
            "missing initialization": log.replace(spill + "\n", "", 1),
            "false spill register": log.replace(spill, spill.replace("src=x20", "src=x21"), 1),
            "missing feature profile": log.replace(feature + "\n", "", 1),
            "missing source phase": log.replace("guest_offset=7", "guest_offset=6"),
            "outside spill frame": log.replace("role=RAX location=sp+0", "role=RAX location=sp+2048", 1),
            "destination lost before fault": log.replace("role=RAX location=sp+0", "role=RAX location=x4", 1),
            "wrong carry recipe": log.replace("carry_inverted=0", "carry_inverted=1"),
            "wrong FP authority": log.replace("native_fpsr=0", "native_fpsr=1"),
            "wrong allocated frame": log.replace("slots=24 bytes=768", "slots=23 bytes=736"),
            "producer rejection": log + "\nNATEMU_REJECT unsupported\n",
            "missing completion": re.sub(r"NATEMU_COMPLETED[^\n]*\n?", "", log),
            "wrong access identity": log.replace("role=RAX", "role=RAX_BAD", 1),
        }
        for label, corrupted in changes.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                parse(corrupted, raw, 1, source)

    def test_corrupt_native_bytes_rejected(self):
        log, raw, source = self.load()
        changes = {"load replaced": (160, 0xD503201F), "store replaced": (308, 0xD503201F),
                   "spill replaced": (60, 0xD503201F), "interior stack adjustment": (156, 0xD10043FF),
                   "unsupported control flow": (156, 0xD4200000), "wrong source computation": (16, 0x4E31D610),
                   "missing restoration": (316, 0xD503201F)}
        for label, (offset, instruction) in changes.items():
            corrupted = bytearray(raw)
            struct.pack_into("<I", corrupted, offset, instruction)
            with self.subTest(label=label), self.assertRaises(ValueError):
                parse(log, bytes(corrupted), 1, source)
        with self.assertRaises(ValueError):
            parse(log, raw[:-4], 1, source)
        with self.assertRaises(ValueError):
            parse(log, raw, 0, source)
        with self.assertRaises(ValueError):
            parse(log, raw, 1, FP_ALIAS)


if __name__ == "__main__":
    unittest.main()

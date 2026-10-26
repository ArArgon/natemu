import copy
import unittest

import i4v_off_check as oracle
from i4v_check import native_seed


def synthetic(scenario="cold", rip=oracle.SOURCE, mutation=-1):
    initial, bank = oracle.native_seed()
    before = oracle.guest_seed(initial, rip)
    rejected = mutation >= 0
    guest, memory = oracle.guest_effects(initial, rip, 0 if rejected else 9)
    faults = int(not rejected and scenario != "warm")
    capture, nonce = (0 if rejected else oracle.PRIVATE + 0xA0080), 0x123456789ABCDEF
    count = mutation // 16 + 1 if rejected else 3
    requests = [oracle.request_packet(nonce, rip, chunk) for chunk in range(count)]
    replies = [oracle.reply_packet(nonce, rip, chunk) for chunk in range(count)]
    if rejected:
        replies[-1][mutation % 16] ^= 1
    entered = copy.deepcopy(initial)
    if not rejected:
        entered["x"][16], entered["x"][30] = oracle.CODE, capture
    lower_before, lower_after = oracle.native_tables(scenario, not rejected)
    root = [0] * 512
    root[256] = oracle.IPA + 0x18000 + 3
    links = [oracle.IPA + offset + 3 for offset in (0x18000, 0x19000, 0x1A000)]
    record = {
        "case": "fex_moffs_load32", "scenario": scenario, "rip": rip, "live": 1,
        "requests": 1, "request": list(requests[-1]), "reply": list(replies[-1]), "vm_nonce": nonce,
        "service_pc": oracle.PRIVATE + 0xA0180, "reply_mutation": mutation,
        "code_before": [0] * 68, "code_after": [0] * 68 if rejected else list(oracle.BODY + oracle.RETURN),
        "body_hex": oracle.BODY.hex(), "initial": initial, "entered": entered,
        "final_frame": oracle.zero_frame() if rejected else oracle.native_effects(initial, capture, True),
        "status": 2 if rejected else 1, "detail": 204 if rejected else 0,
        "current_el": 4, "sctlr": 0x30D01985, "actlr": 2, "tcr": oracle.TCR,
        "ttbr1": oracle.IPA + 0x17000, "completed": 0 if rejected else 9, "operand": 0,
        "width": 0, "stage": 0 if rejected else 2, "memory_before": [0xA5] * 4096,
        "memory_after": memory, "data_page_before": [0xA5] * 4096, "data_page_after": list(memory), "bank_before": bank,
        "recovery": {"capture_pc": capture, "guest_before": before, "guest_after": guest,
                     "control": [0x1000, 0, 1, 0, 0], "faults": faults, "walks": 0,
                     "retries": faults, "vector": 0, "error": 0, "linear": 0, "rip": 0,
                     "traces": [], "tables_before": [0xA4] * 16384, "tables_after": [0xA4] * 16384,
                     "memory_after": memory[:256], "bank_after": list(bank)},
        "upper_root_before": root, "upper_root_after": list(root), "upper_leaf_before": [0] * 512,
        "upper_leaf_after": [0] * 512, "upper_links_before": links, "upper_links_after": list(links),
        "lower_leaf_before": lower_before, "lower_leaf_after": lower_after,
        "off": {"a20": 1, "scenario": ("cold", "readonly", "warm").index(scenario), "transfers": count,
                "fetch_gpa": rip, "repair_gpa": oracle.DATA if faults else 0,
                "data_before_retry": [0xA5] * 16 if faults else [0] * 16,
                "requests": requests + [[0] * 16 for _ in range(3 - count)],
                "replies": replies + [[0] * 16 for _ in range(3 - count)],
                "prefix": oracle.guest_effects(initial, rip, 2)[0] if faults else oracle.zeros(before)},
    }
    if faults:
        record["recovery"]["traces"].append({"frame": oracle.native_effects(initial, capture, False),
            "esr": (0x25 << 26) | 64 | (7 if scenario == "cold" else 15), "far": oracle.DATA,
            "elr": oracle.CODE + 16, "spsr": initial["nzcv"] | 0x3C4, "outcome": 1, "error": 0})
    return record


def change(path, value):
    def mutate(record):
        target = record
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return mutate


class Off32Tests(unittest.TestCase):
    def test_six_execution_scenarios_and_all_reply_rejections(self):
        for scenario in ("cold", "readonly", "warm"):
            for rip in (oracle.SOURCE, oracle.SOURCE + 0x80):
                with self.subTest(scenario=scenario, rip=rip):
                    oracle.check(synthetic(scenario, rip), scenario, rip)
        for mutation in range(48):
            with self.subTest(mutation=mutation):
                oracle.check(synthetic(mutation=mutation))

    def test_fabricated_state_faults_routing_and_writes_fail(self):
        corruptions = [
            ("paging enabled", change(["recovery", "guest_before", "control", 0], 0x80010001)),
            ("A20 off", change(["off", "a20"], 0)),
            ("nonflat DS", change(["recovery", "guest_before", "segments", 1, 1], 4096)),
            ("long CS", change(["recovery", "guest_before", "segments", 0, 3], 0xA09B)),
            ("native input wrong width", change(["initial", "x", 4], 0x1234000004040404)),
            ("wrong partial merge", change(["recovery", "guest_after", "gpr", 0], 0x42)),
            ("wrong final ECX", change(["recovery", "guest_after", "gpr", 1], 0xFFFFFFFF)),
            ("unrelated canonical state", change(["recovery", "guest_after", "gpr", 15], 0)),
            ("unexpected CR2", change(["recovery", "guest_after", "control", 1], oracle.DATA)),
            ("arithmetic flags clobbered", change(["recovery", "guest_after", "rflags"], 2)),
            ("final scratch x21 lost", change(["final_frame", "x", 21], 0)),
            ("final saved accumulator lost", change(["final_frame", "x", 22], 0)),
            ("native vector corruption", change(["final_frame", "q", 31, 15], 0)),
            ("page walker invoked", change(["recovery", "walks"], 1)),
            ("implicit A bit", change(["recovery", "tables_after", 0], 0xA6)),
            ("implicit D bit", change(["recovery", "tables_after", 0x3800], 0xE4)),
            ("native miss became guest PF", change(["recovery", "vector"], 14)),
            ("native repair wrong GPA", change(["off", "repair_gpa"], oracle.DATA + 4096)),
            ("source identity wrong", change(["off", "fetch_gpa"], oracle.SOURCE + 0x80)),
            ("private backing leaked", change(["off", "repair_gpa"], oracle.PRIVATE + 0x70000)),
            ("prefix reset", change(["off", "prefix", "gpr", 0], 0)),
            ("prefix ahead of fault", change(["off", "prefix", "rip"], oracle.SOURCE + 12)),
            ("store committed before retry", change(["off", "data_before_retry", 0], 0x42)),
            ("missing native fault", change(["recovery", "traces"], [])),
            ("wrong native fault PC", change(["recovery", "traces", 0, "elr"], oracle.CODE + 24)),
            ("wrong native FAR", change(["recovery", "traces", 0, "far"], oracle.DATA + 4)),
            ("fault scratch wrong", change(["recovery", "traces", 0, "frame", "x", 20], 0x42)),
            ("fault class wrong", change(["recovery", "traces", 0, "esr"], (0x25 << 26) | 64 | 15)),
            ("missing retry", change(["recovery", "retries"], 0)),
            ("unowned physical write", change(["data_page_after", 4095], 0)),
            ("wide store", change(["memory_after", 4], 0)),
            ("source native mapping lost", change(["lower_leaf_after", 258], 0)),
            ("unexpected upper mapping", change(["upper_leaf_after", 256], 1)),
            ("wrong code", change(["code_after", 0], 0)),
            ("compiler continuation in private data", change(["service_pc"], oracle.PRIVATE + 0x41000)),
            ("boolean counter", change(["requests"], True)),
        ]
        for name, mutate in corruptions:
            with self.subTest(corruption=name):
                record = synthetic()
                mutate(record)
                with self.assertRaises(ValueError):
                    oracle.check(record)
        for chunk in range(3):
            for word in range(16):
                for field in ("requests", "replies"):
                    record = synthetic()
                    record["off"][field][chunk][word] ^= 1
                    with self.subTest(field=field, chunk=chunk, word=word), self.assertRaises(ValueError):
                        oracle.check(record)
        for field, path in (("unpublished body", ["code_after", 0]),
                            ("fabricated rejected execution", ["completed"]),
                            ("rejected store", ["data_page_after", 0])):
            record = synthetic(mutation=47)
            change(path, 1)(record)
            with self.subTest(corruption=field), self.assertRaises(ValueError):
                oracle.check(record)

    def test_literal_upstream_prefixes_and_partial_accumulator(self):
        registers = {"RAX": 0x12345678, "RCX": 0x87654321, "RDX": 99, "RFLAGS": 0xAD7}
        memory = dict(enumerate([0xA5] * 256, oracle.DATA))
        expected = [
            (0x12345678, 0x87654321, 99),
            (0x12345678, 0x87654321, 0x100000),
            (0x42, 0x87654321, 0x100000),
            (0x42, 0x87654321, 0x100000),
            (0xFFFFFFFF, 0x87654321, 0x100000),
            (0x42, 0x87654321, 0x100000),
            (0x42, 0x87654321, 0x42),
            (0xFFFFFFFF, 0x87654321, 0x42),
            (0xFFFF0042, 0x87654321, 0x42),
            (0xFFFF0042, 0xFFFF0042, 0x42),
        ]
        for prefix, values in enumerate(expected):
            regs, after = oracle.architectural_effects(registers, memory, prefix)
            self.assertEqual(tuple(regs[k] for k in ("RAX", "RCX", "RDX")), values)
            self.assertEqual(regs["RFLAGS"], 0xAD7)
            wanted = ([0x42, 0, 0, 0] if prefix >= 3 else [0xA5] * 4) + [0xA5] * 252
            self.assertEqual([after[oracle.DATA + i] for i in range(256)], wanted)
        self.assertEqual(registers["RAX"], 0x12345678)
        self.assertEqual(list(memory.values()), [0xA5] * 256)

    def test_source_identity_and_three_exact_body_chunks(self):
        for rip in (0x102000, 0x102080):
            chunks = []
            for chunk, length in enumerate((24, 24, 16)):
                request = oracle.request_packet(37, rip, chunk)
                self.assertEqual(request[:9], [0x49345621, 1, 37, 0, 1, 1, 1, rip, 32])
                source = b"".join(word.to_bytes(8, "little") for word in request[9:14])
                self.assertEqual(source, oracle.SOURCE_BYTES + bytes(3))
                self.assertEqual(request[14:], [1, chunk])
                reply = oracle.reply_packet(37, rip, chunk)
                self.assertEqual(reply[:9], [0x49345622, *request[1:9]])
                self.assertEqual(reply[12:], [chunk, length, 1, 64])
                part = b"".join(word.to_bytes(8, "little") for word in reply[9:12])
                self.assertEqual(part[length:], bytes(24 - length))
                chunks.append(part[:length])
            self.assertEqual(b"".join(chunks), oracle.BODY)

    def test_native_first_store_prefix_and_preserved_roots(self):
        initial = native_seed("low_operand")
        capture = oracle.PRIVATE + 0xA0080
        fault = oracle.native_effects(initial, capture, False)
        final = oracle.native_effects(initial, capture, True)
        expected_fault = copy.deepcopy(initial)
        for reg, value in {4: 0x42, 5: 0x100000, 16: oracle.CODE, 20: 0x100000, 30: capture}.items():
            expected_fault["x"][reg] = value
        self.assertEqual(fault, expected_fault)
        expected_final = copy.deepcopy(initial)
        for reg, value in {4: 0xFFFF0042, 5: 0x42, 7: 0xFFFF0042, 16: oracle.CODE,
                           20: 0x42, 21: 0x42, 22: 0xFFFFFFFF, 30: capture}.items():
            expected_final["x"][reg] = value
        self.assertEqual(final, expected_final)
        self.assertEqual(len(oracle.BODY), 64)
        self.assertEqual(oracle.BODY[16:20].hex(), "a4fc9f88")
        self.assertEqual(oracle.BODY[24:28].hex(), "95fedf88")
        self.assertEqual(oracle.BODY[48:52].hex(), "94fedf48")
        self.assertEqual(oracle.GUEST_OFFSETS[-1], len(oracle.SOURCE_BYTES))


if __name__ == "__main__":
    unittest.main()

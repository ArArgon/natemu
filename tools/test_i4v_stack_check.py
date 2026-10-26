import copy
import unittest

import i4v_stack_check as oracle


def synthetic(scenario="cold", rip=oracle.HIGH + oracle.SOURCE, mutation=-1):
    """Expected-shape reports for corruption tests, not execution evidence."""
    expected = oracle.outcomes(scenario, mutation)
    completed, sites = expected["completed"], expected["sites"]
    vector, error = expected["vector"], expected["error"]
    due, requested, rejected = expected["due"], expected["requested"], expected["rejected"]
    nonce = 0x123456789ABCDEF
    capture = oracle.PRIVATE + 0xA0080 if any(expected["entries"]) else 0
    prefixes = oracle.native_prefixes(scenario, capture)
    entered, final = oracle.native_seed(scenario), oracle.zero_frame()
    if any(expected["entries"]):
        last = max(step for step, count in enumerate(expected["entries"]) if count)
        entered = oracle.native_entry(scenario, last, capture)
        if last < completed:
            final = copy.deepcopy(prefixes[last])
            if completed < 5:
                entered = copy.deepcopy(final)
        else:
            final = oracle.native_fault(scenario, last, capture)
    guest, memory = oracle.architectural_prefix(scenario, rip, completed)
    linear = 0 if not vector else rip if scenario in ("fetch_nx", "smep") else oracle.address(scenario, completed)
    if vector == 14:
        guest["control"][1] = linear
    tables_before, tables_after = oracle.guest_tables(scenario, rip, completed, sites)
    lower_before, lower_after, upper_after = oracle.native_leaves(scenario, completed)
    root = [0] * 512
    root[256] = (oracle.IPA + 0x18000) | 3
    links = [(oracle.IPA + page) | 3 for page in (0x18000, 0x19000, 0x1A000)]
    bank = [(i * 37 + 19) % 256 for i in range(512)]
    requests, replies = oracle.packets(nonce, rip, expected["transfers"], mutation)
    traces = []
    for step in sites:
        permitted = step < completed
        traces.append({"frame": oracle.native_fault(scenario, step, capture),
                       "esr": (0x25 << 26) | (int(step < 3) << 6) | 7,
                       "far": oracle.address(scenario, step),
                       "elr": oracle.CODE + step * 64 + oracle.SITE_OFFSETS[step],
                       "spsr": 0xA00003C4, "outcome": 1 if permitted else 2,
                       "error": 0 if permitted else error})
    return {
        "case": "fex_push_immediates64", "scenario": scenario, "rip": rip,
        "live": 1, "requests": requested, "request": list(requests[0]), "reply": list(replies[0]),
        "vm_nonce": nonce, "service_pc": oracle.PRIVATE + 0xA0180 if requested else 0,
        "reply_mutation": mutation, "code_before": [0] * 320,
        "code_after": oracle.code_image() if requested and not rejected else [0] * 320,
        "body_hex": oracle.BODY.hex(), "initial": oracle.native_seed(scenario),
        "entered": entered, "final_frame": final,
        "status": 2 if rejected else 1, "detail": 115 if rejected else 0,
        "current_el": 4, "sctlr": 0x30D01985, "actlr": 2, "tcr": oracle.TCR,
        "ttbr1": oracle.IPA + 0x17000, "completed": completed,
        "operand": 0 if due is None else oracle.address(scenario, due),
        "width": 0 if due is None else oracle.WIDTHS[due],
        "stage": 0 if rejected else 2 if completed == 5 else 3,
        "memory_before": [0xA5] * 8192, "memory_after": memory,
        "bank_before": bank,
        "recovery": {"capture_pc": capture, "guest_before": oracle.guest_seed(scenario, rip),
                     "guest_after": guest, "control": [0x1000, 3 if scenario in ("user_ro", "user_denied", "user_ok") else 0,
                                                        0 if scenario in ("wp_off", "user_ro") else 1, 1, int(scenario == "smep")],
                     "faults": len(sites), "walks": 1 + len(sites), "retries": expected["retries"],
                     "vector": vector, "error": error, "linear": linear,
                     "rip": rip + oracle.GUEST_OFFSETS[completed] if vector else 0,
                     "traces": traces, "tables_before": tables_before, "tables_after": tables_after,
                     "memory_after": list(memory[:256]), "bank_after": list(bank)},
        "upper_root_before": list(root), "upper_root_after": list(root),
        "upper_links_before": list(links), "upper_links_after": list(links),
        "upper_leaf_before": [0] * 512, "upper_leaf_after": upper_after,
        "lower_leaf_before": lower_before, "lower_leaf_after": lower_after,
        "stack": {"transfers": expected["transfers"], "entries": expected["entries"],
                  "completions": expected["completions"], "requests": requests, "replies": replies,
                  "prefixes": prefixes[:completed] + [oracle.zero_frame() for _ in range(5 - completed)]},
    }


def change(path, value):
    def mutate(cell):
        target = cell
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value
    return mutate


class StackOracleTests(unittest.TestCase):
    def test_all_scenarios_and_reply_rejections(self):
        for rip in (oracle.SOURCE, oracle.HIGH + oracle.SOURCE):
            for scenario in oracle.CASES:
                with self.subTest(scenario=scenario, rip=hex(rip)):
                    oracle.check(synthetic(scenario, rip))
            for mutation in range(48):
                with self.subTest(mutation=mutation, rip=hex(rip)):
                    oracle.check(synthetic(rip=rip, mutation=mutation))

    def test_upstream_stack_bytes_and_partial_commits(self):
        for scenario in ("cold", "low_stack", "wp_off", "user_ok"):
            guest, memory = oracle.architectural_prefix(scenario, oracle.SOURCE, 5)
            self.assertEqual(guest["gpr"][0], 0xFFFFFFFFBEBDBCBB)
            self.assertEqual(guest["gpr"][3], 0x51526162)
            self.assertEqual(guest["gpr"][4], oracle.stack_top(scenario) - 12)
            self.assertEqual(bytes(memory[4092:4104]).hex(), "62615251bbbcbdbeffffffff")
            self.assertEqual(memory[:4092] + memory[4104:], [0xA5] * (8192 - 12))
        for scenario in ("ro_after_first", "np_after_first"):
            guest, memory = oracle.architectural_prefix(scenario, oracle.SOURCE, 1)
            self.assertEqual(guest["gpr"][4], oracle.stack_top(scenario) - 8)
            self.assertEqual(bytes(memory[4096:4104]).hex(), "bbbcbdbeffffffff")
            self.assertEqual(memory[:4096], [0xA5] * 4096)
        _, memory = oracle.architectural_prefix("ss_after_first", oracle.SOURCE, 1)
        self.assertEqual(bytes(memory[:8]).hex(), "bbbcbdbeffffffff")
        self.assertEqual(memory[8:], [0xA5] * (8192 - 8))

    def test_precise_fault_native_writeback_and_scratch(self):
        for step, scratch, decrement in ((0, 0xFFFFFFFFBEBDBCBB, 0), (1, 0x5152, 8), (2, 0x6162, 10)):
            frame = oracle.native_fault("cold", step, oracle.PRIVATE + 0xA0080)
            self.assertEqual(frame["x"][8], oracle.stack_top("cold") - decrement)
            self.assertEqual(frame["x"][20], scratch)
            self.assertNotEqual(frame["x"][8], oracle.address("cold", step))
            self.assertEqual(frame["sp"], oracle.PRIVATE + 0x54000)

    def test_state_and_memory_corruptions_rejected(self):
        mutations = [
            ("false success", "ro_first", change(["status"], 2)),
            ("wrong program", "cold", change(["case"], "fex_memory_data64")),
            ("guest RSP clobber", "cold", change(["recovery", "guest_after", "gpr", 4], 0)),
            ("guest register clobber", "cold", change(["recovery", "guest_after", "gpr", 15], 0)),
            ("guest flags", "cold", change(["recovery", "guest_after", "rflags"], 0)),
            ("guest upper YMM", "cold", change(["recovery", "guest_after", "ymm", 14, 31], 0)),
            ("lost prior stack store", "ro_after_first", change(["memory_after", 4096], 0xA5)),
            ("failed stack store committed", "ro_after_first", change(["memory_after", 4094], 0x52)),
            ("guard corruption", "cold", change(["memory_after", 8191], 0)),
            ("unrelated native SP", "cold", change(["final_frame", "sp"], oracle.HIGH)),
            ("second segment reset", "cold", change(["stack", "prefixes", 1, "x", 8], oracle.stack_top("cold") - 2)),
            ("prefix missing", "cold", lambda c: c["stack"]["prefixes"].pop()),
            ("duplicate instruction", "cold", change(["stack", "entries", 0], 2)),
            ("boolean count", "cold", change(["stack", "completions", 0], True)),
            ("early RSP writeback", "ro_first", change(["recovery", "traces", 0, "frame", "x", 8], oracle.stack_top("ro_first") - 8)),
            ("scratch not materialized", "ro_first", change(["recovery", "traces", 0, "frame", "x", 20], 0)),
            ("wrong data direction", "cold", change(["recovery", "traces", 0, "esr"], (0x25 << 26) | 7)),
            ("incorrect load direction", "read_np_after_pushes", change(["recovery", "traces", 2, "esr"], (0x25 << 26) | 64 | 7)),
            ("SS changed CR2", "ss_after_first", change(["recovery", "guest_after", "control", 1], oracle.HIGH - 2)),
            ("GP changed CR2", "gp_after_pushes", change(["recovery", "guest_after", "control", 1], oracle.PRIVATE)),
            ("SS became GP", "ss_span", change(["recovery", "vector"], 13)),
            ("late fault lost prefix RIP", "gp_after_pushes", change(["recovery", "rip"], oracle.HIGH + oracle.SOURCE)),
            ("denied leaf dirty", "ro_first", lambda c: c["recovery"]["tables_after"].__setitem__(0x3808, c["recovery"]["tables_after"][0x3808] | 64)),
            ("first write lacks dirty", "cold", lambda c: c["recovery"]["tables_after"].__setitem__(0x3808, c["recovery"]["tables_after"][0x3808] & ~64)),
            ("spurious low root A", "cold", lambda c: c["recovery"]["tables_after"].__setitem__(0, c["recovery"]["tables_after"][0] | 32)),
            ("wrong native page", "cold", change(["upper_leaf_after", 255], 7)),
            ("wrong regime", "cold", change(["ttbr1"], oracle.IPA + 0x10000)),
            ("source alias lost", "cold", change(["lower_leaf_after", 258], 0)),
            ("private stack executed", "ss_first", change(["stack", "entries", 0], 1)),
            ("late guarded step executed", "ss_after_first", change(["stack", "entries", 1], 1)),
            ("fetch failure compiled", "fetch_nx", change(["requests"], 1)),
            ("published initial code", "cold", change(["code_before", 0], 0x94)),
            ("bad segment return", "cold", change(["code_after", 12], 0)),
            ("segment garbage", "cold", change(["code_after", 63], 1)),
        ]
        for name, scenario, mutate in mutations:
            with self.subTest(corruption=name):
                cell = synthetic(scenario)
                mutate(cell)
                with self.assertRaises(ValueError):
                    oracle.check(cell)

    def test_every_packet_word_and_rejected_publication(self):
        for chunk in range(3):
            for field in ("requests", "replies"):
                for word in range(16):
                    with self.subTest(chunk=chunk, field=field, word=word):
                        cell = synthetic()
                        cell["stack"][field][chunk][word] ^= 1
                        with self.assertRaises(ValueError):
                            oracle.check(cell)
        for mutation in range(48):
            for path, value in ((["code_after", 0], 0x94), (["completed"], 1),
                                (["stack", "transfers"], 0), (["status"], 1),
                                (["recovery", "guest_after", "gpr", 4], 0),
                                (["upper_leaf_after", 257], 7)):
                with self.subTest(mutation=mutation, path=path):
                    cell = synthetic(mutation=mutation)
                    change(path, value)(cell)
                    with self.assertRaises(ValueError):
                        oracle.check(cell)

    def test_matching_corrupt_seeds_rejected(self):
        cell = synthetic()
        for key in ("guest_before", "guest_after"):
            cell["recovery"][key]["gpr"][15] = 123
        with self.assertRaises(ValueError):
            oracle.check(cell)


if __name__ == "__main__":
    unittest.main()

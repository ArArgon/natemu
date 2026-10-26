import copy
import unittest

import i4v_check as oracle


def synthetic(scenario="cold", rip=oracle.HIGH + oracle.SOURCE, live=None, mutation=-1):
    """A contract-shaped report, not an execution artifact."""
    if scenario == "noncanonical_rip":
        rip = 0x0000800000102000
    completed, faults, walks, retries, vector, error = oracle.CASES[scenario]
    rejected = mutation >= 0
    if rejected:
        completed, faults, walks, retries, vector, error = 0, 0, 1, 0, 0, 0
    capture = oracle.PRIVATE + 0x1000 if faults else 0
    entered = oracle.native_seed(scenario) if rejected else oracle.entered_frame(scenario, capture)
    final = copy.deepcopy(entered) if faults else oracle.zero_frame()
    if completed:
        final["x"][4] = 0xDDCCBBAA
    before, after = oracle.guest_tables(scenario, rip, execute=not rejected)
    bank = [(i * 37 + 19) % 256 for i in range(512)]
    root, leaves = [0] * 512, [0] * 512
    root[256] = (oracle.IPA + 0x18000) | 3
    links = [(oracle.IPA + address) | 3 for address in (0x18000, 0x19000, 0x1A000)]
    cell = {
        "scenario": scenario, "rip": rip, "body_hex": oracle.BODY,
        "initial": oracle.native_seed(scenario), "entered": entered, "final_frame": final,
        "status": 2 if rejected else 1, "detail": 100 if rejected else 0, "current_el": 4, "sctlr": 0x30D01985, "actlr": 2,
        "tcr": oracle.TCR, "ttbr1": oracle.IPA + 0x17000,
        "completed": completed, "operand": oracle.operand(scenario), "width": 8,
        "stage": 0 if rejected else 2 if completed else 3, "memory_before": oracle.data_seed(), "bank_before": bank,
        "recovery": {
            "capture_pc": capture, "guest_before": oracle.guest_seed(scenario, rip),
            "guest_after": oracle.guest_seed(scenario, rip) if rejected else oracle.expected_guest(scenario, rip),
            "control": [0x1000, 3 if scenario == "user_denied" else 0, 1, 1, int(scenario == "smep")],
            "faults": faults, "walks": walks, "retries": retries, "vector": vector, "error": error,
            "linear": 0 if not vector else rip if scenario in ("fetch_nx", "smep", "noncanonical_rip") else oracle.operand(scenario),
            "rip": rip if vector else 0, "traces": [], "tables_before": before, "tables_after": after,
            "memory_after": oracle.data_seed(), "bank_after": list(bank),
        },
        "upper_root_before": list(root), "upper_root_after": list(root),
        "upper_links_before": list(links), "upper_links_after": list(links),
        "upper_leaf_before": list(leaves), "upper_leaf_after": list(leaves),
        "lower_data_leaf_before": 0,
        "lower_data_leaf_after": oracle.READ_ONLY_DATA if scenario == "low_operand" else 0,
    }
    if completed and scenario != "low_operand":
        cell["upper_leaf_after"][256] = oracle.READ_ONLY_DATA
    if faults:
        cell["recovery"]["traces"].append({
            "frame": copy.deepcopy(entered), "esr": 0x25 << 26 | 7,
            "far": oracle.operand(scenario), "elr": oracle.CODE,
            "spsr": 0xA00003C4, "outcome": 1 if completed else 2, "error": error,
        })
    if live is not None:
        nonce = 0x123456789ABCDEF if live else 0
        requested = int(bool(live and oracle.CASES[scenario][1]))
        request = [0x49345601, 1, nonce, 0, 1, 1, 1, rip, 64, 3, 0x038B48, 0, 0, 1, 0, 0] if requested else [0] * 16
        reply = list(request)
        if requested:
            reply[0], reply[11], reply[12] = 0x49345602, 8, 0xD503201FC8DFFCC4
            if rejected:
                reply[mutation] ^= 1
        code = list(bytes.fromhex(oracle.BODY + "c0035fd6"))
        cell.update(live=live, vm_nonce=nonce, requests=requested, request=request, reply=reply,
                    service_pc=oracle.PRIVATE + 0x1100 if requested else 0,
                    reply_mutation=mutation, code_before=[0] * 12 if live else code,
                    code_after=code if not live or (requested and not rejected) else [0] * 12)
    return cell


def change(path, value):
    def mutate(cell):
        target = cell
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return mutate


class I4VCheckTests(unittest.TestCase):
    def test_all_scenarios_high_and_low_source(self):
        for scenario in oracle.CASES:
            for rip in (oracle.SOURCE, oracle.HIGH + oracle.SOURCE):
                with self.subTest(scenario=scenario, rip=hex(rip)):
                    oracle.check(synthetic(scenario, rip))

    def test_upstream_load_expectation_and_fault_preservation(self):
        before = oracle.guest_seed("cold", oracle.HIGH + oracle.SOURCE)
        after = oracle.expected_guest("cold", oracle.HIGH + oracle.SOURCE)
        self.assertEqual(after["gpr"][0], 0xDDCCBBAA)
        after["gpr"][0] = before["gpr"][0]
        after["rip"] -= 3
        self.assertEqual(after, before)
        for scenario in ("private_operand", "noncanonical_operand", "noncanonical_rip"):
            self.assertEqual(oracle.expected_guest(scenario, oracle.SOURCE), oracle.guest_seed(scenario, oracle.SOURCE))

    def test_corrupted_reports_fail(self):
        corruptions = [
            ("wrong result", "cold", change(["recovery", "guest_after", "gpr", 0], 0)),
            ("unrelated GPR", "cold", change(["recovery", "guest_after", "gpr", 15], 0)),
            ("flags", "cold", change(["recovery", "guest_after", "rflags"], 0)),
            ("upper YMM", "cold", change(["recovery", "guest_after", "ymm", 15, 31], 0)),
            ("x87", "cold", change(["recovery", "guest_after", "x87", 0], 0)),
            ("segment", "cold", change(["recovery", "guest_after", "segments", 7, 2], 0)),
            ("missing guest array entry", "cold", lambda c: c["recovery"]["guest_after"]["gpr"].pop()),
            ("boolean counter", "cold", change(["completed"], True)),
            ("unexecuted body", "cold", change(["completed"], 0)),
            ("wrong bytes", "cold", change(["body_hex"], "1f2003d5")),
            ("source RIP truncated", "cold", change(["recovery", "guest_after", "rip"], oracle.SOURCE + 3)),
            ("native RAX unchanged", "cold", change(["final_frame", "x", 4], 0)),
            ("native clobber", "cold", change(["final_frame", "x", 28], 0)),
            ("fault snapshot prematurely committed", "cold", change(["recovery", "traces", 0, "frame", "x", 4], 0xDDCCBBAA)),
            ("no retry", "cold", change(["recovery", "retries"], 0)),
            ("no native fault", "cold", change(["recovery", "traces"], [])),
            ("write syndrome", "cold", change(["recovery", "traces", 0, "esr"], 0x25 << 26 | 64 | 7)),
            ("wrong fault PC", "cold", change(["recovery", "traces", 0, "elr"], oracle.CODE + 4)),
            ("dirty leaf", "cold", lambda c: c["recovery"]["tables_after"].__setitem__(0x3800, c["recovery"]["tables_after"][0x3800] | 64)),
            ("unvisited low root accessed", "cold", lambda c: c["recovery"]["tables_after"].__setitem__(0, c["recovery"]["tables_after"][0] | 32)),
            ("missing high source accessed", "cold", change(["recovery", "tables_after", 0x800], 7)),
            ("extra mapping", "cold", change(["upper_leaf_after", 257], oracle.READ_ONLY_DATA)),
            ("writable native load route", "cold", change(["upper_leaf_after", 256], oracle.READ_ONLY_DATA & ~(1 << 7))),
            ("high alias mapped low", "cold", change(["lower_data_leaf_after"], oracle.READ_ONLY_DATA)),
            ("upper root wrong slot", "cold", change(["upper_root_after", 0], (oracle.IPA + 0x18000) | 3)),
            ("native upper regime disabled", "cold", change(["tcr"], oracle.TCR | 1 << 23)),
            ("wrong TTBR1", "cold", change(["ttbr1"], oracle.IPA + 0x10000)),
            ("memory changed", "cold", change(["recovery", "memory_after", 255], 0)),
            ("bank changed", "cold", change(["recovery", "bank_after", 511], 0)),
            ("wrong PF error", "user_denied", change(["recovery", "error"], 0)),
            ("wrong PF address", "load_np", change(["recovery", "guest_after", "control", 1], oracle.DATA)),
            ("PF instruction committed", "load_np", change(["recovery", "guest_after", "gpr", 0], 0xDDCCBBAA)),
            ("GP changed CR2", "private_operand", change(["recovery", "guest_after", "control", 1], oracle.PRIVATE)),
            ("GP executed native", "private_operand", change(["recovery", "capture_pc"], oracle.PRIVATE + 0x1000)),
            ("fetch failure did data walk", "fetch_nx", change(["recovery", "walks"], 2)),
            ("noncanonical RIP did table walk", "noncanonical_rip", change(["recovery", "walks"], 1)),
        ]
        for name, scenario, mutate in corruptions:
            with self.subTest(corruption=name):
                cell = synthetic(scenario)
                mutate(cell)
                with self.assertRaises(ValueError):
                    oracle.check(cell)

    def test_matching_corrupt_input_output_rejected(self):
        cell = synthetic()
        cell["recovery"]["guest_before"]["gpr"][15] = 123
        cell["recovery"]["guest_after"]["gpr"][15] = 123
        with self.assertRaises(ValueError):
            oracle.check(cell)

    def test_live_and_eager_service_metadata(self):
        for scenario in oracle.CASES:
            for live in (0, 1):
                for rip in (oracle.SOURCE, oracle.HIGH + oracle.SOURCE):
                    with self.subTest(scenario=scenario, live=live, rip=hex(rip)):
                        oracle.check(synthetic(scenario, rip, live))

    def test_service_corruption_rejected(self):
        mutations = [
            ("missing metadata", "cold", lambda c: c.pop("reply")),
            ("missing code evidence", "cold", lambda c: c.pop("code_before")),
            ("not live", "cold", change(["live"], 0)),
            ("boolean mode", "cold", change(["live"], True)),
            ("zero nonce", "cold", change(["vm_nonce"], 0)),
            ("duplicate request", "cold", change(["requests"], 2)),
            ("missing request", "cold", change(["requests"], 0)),
            ("invalid continuation", "cold", change(["service_pc"], oracle.CODE)),
            ("misaligned continuation", "cold", change(["service_pc"], oracle.PRIVATE + 1)),
            ("extra packet word", "cold", lambda c: c["request"].append(0)),
            ("no compile before data fault", "load_np", change(["request"], [0] * 16)),
            ("compile before fetch failure", "fetch_nx", change(["requests"], 1)),
            ("compile before private exclusion", "private_operand", change(["requests"], 1)),
            ("reply after fetch failure", "smep", change(["reply", 12], 0xD503201FC8DFFCC4)),
            ("prepublished live code", "cold", change(["code_before", 0], 0xC4)),
            ("wrong published code", "cold", change(["code_after", 0], 0)),
            ("published code after fetch failure", "fetch_nx", change(["code_after", 0], 0xC4)),
        ]
        for packet in ("request", "reply"):
            for index in range(16):
                mutations.append((f"{packet} word {index}", "cold", lambda c, packet=packet, index=index: c[packet].__setitem__(index, c[packet][index] ^ 1)))
        for name, scenario, mutate in mutations:
            with self.subTest(corruption=name):
                cell = synthetic(scenario, live=1)
                mutate(cell)
                with self.assertRaises(ValueError):
                    oracle.check(cell)
        cell = synthetic(live=1)
        cell["request"][7] = cell["reply"][7] = oracle.SOURCE
        with self.assertRaises(ValueError):
            oracle.check(cell)
        eager = synthetic(live=0)
        eager["vm_nonce"] = 1
        with self.assertRaises(ValueError):
            oracle.check(eager)

    def test_each_mutated_reply_rejected_before_publication(self):
        for mutation in range(16):
            for rip in (oracle.SOURCE, oracle.HIGH + oracle.SOURCE):
                with self.subTest(mutation=mutation, rip=hex(rip)):
                    cell = synthetic(rip=rip, live=1, mutation=mutation)
                    oracle.check(cell)
                    for path, value in ((["status"], 1), (["detail"], 0), (["stage"], 1),
                                        (["completed"], 1), (["code_after", 0], 0xC4),
                                        (["recovery", "faults"], 1),
                                        (["recovery", "guest_after", "gpr", 0], 0xDDCCBBAA),
                                        (["upper_leaf_after", 256], oracle.READ_ONLY_DATA)):
                        corrupted = copy.deepcopy(cell)
                        change(path, value)(corrupted)
                        with self.assertRaises(ValueError):
                            oracle.check(corrupted)
        for mutation in (-2, 16, True):
            cell = synthetic(live=1)
            cell["reply_mutation"] = mutation
            with self.assertRaises(ValueError):
                oracle.check(cell)
        cell = synthetic(live=0)
        cell["reply_mutation"] = 0
        with self.assertRaises(ValueError):
            oracle.check(cell)


if __name__ == "__main__":
    unittest.main()

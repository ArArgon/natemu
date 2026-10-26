import copy
import unittest

import i4v_off_steps_check as oracle


def synthetic(scenario="segments", mutation=-1):
    nonce = 0x123456789ABCDEF
    completed = oracle.mutation_site(mutation)[0] if mutation >= 0 else oracle.profile(scenario)["completed"]
    capture = oracle.PRIVATE + 0xA0080 if completed else 0
    selected, bad, completed, requested, faulted, prefixes, faults, fault_steps, steps = oracle.expected_progress(scenario, mutation, nonce, capture)
    initial, bank = oracle.native_seed()
    entered = copy.deepcopy(prefixes[-1] if prefixes else initial)
    if completed == 9:
        entered = copy.deepcopy(prefixes[-2])
        entered["x"][16], entered["x"][30] = oracle.CODE + 256, capture
    current = min(completed, 8)
    operand = current in oracle.MEMORY_STEPS and (bad is not None or scenario in ("ds-limit", "ds-readonly"))
    attrs = 3 | (1 << 10) | (3 << 8) | (3 << 53)
    root, lower = [0] * 512, [0] * 512
    root[256] = oracle.IPA + 0x18000 + 3
    lower[258] = oracle.IPA + 0x72000 | attrs
    links = [oracle.IPA + offset + 3 for offset in (0x18000, 0x19000, 0x1A000)]
    final_lower = list(lower)
    latest_memory = max((step for step in oracle.MEMORY_STEPS if step < requested), default=-1)
    if latest_memory >= 0 and latest_memory < completed and scenario not in ("device", "hole"):
        backing = {"a20-off": 0x71000, "linear-wrap": 0x74000}.get(scenario, 0x70000)
        final_lower[256] = oracle.IPA + backing | attrs
    traces = [{"frame": frame, "far": oracle.DATA, "elr": oracle.CODE + step * 32 + 4,
               "outcome": 4 if scenario in ("device", "hole") else 1, "error": 0,
               "esr": (0x25 << 26) | (int(step == 2) << 6) | 7, "spsr": initial["nzcv"] | 0x3C4}
              for frame, step in zip(faults, fault_steps)]
    return {"case": "fex_moffs_load32_steps", "scenario": scenario, "rip": oracle.EIP,
            "live": 1, "vm_nonce": nonce, "reply_mutation": mutation,
            "requests": requested, "request": steps["requests"][requested - 1] if requested else [0] * 16,
            "reply": steps["replies"][requested - 1] if requested else [0] * 16,
            "service_pc": oracle.PRIVATE + 0xA0180 if requested else 0,
            "code_before": [0] * 288, "code_after": oracle.code_image(completed),
            "status": 2 if bad else 1, "detail": 224 if bad else 0,
            "current_el": 4, "sctlr": 0x30D01985, "actlr": 2, "tcr": oracle.TCR, "ttbr1": oracle.IPA + 0x17000,
            "completed": completed, "stage": 0 if bad else 3 if faulted else 2,
            "operand": oracle.DATA if operand else 0, "width": oracle.WIDTHS[current] if operand else 0,
            "initial": initial, "entered": entered, "final_frame": prefixes[-1] if prefixes else oracle.zero_frame(),
            "steps": steps, "data_pages_before": oracle.data_pages(scenario, 0),
            "data_pages_after": oracle.data_pages(scenario, completed),
            "source_pages_before": oracle.source_pages(scenario), "source_pages_after": oracle.source_pages(scenario), "bank_before": bank,
            "upper_root_before": root, "upper_root_after": list(root),
            "upper_links_before": links, "upper_links_after": list(links),
            "upper_leaf_before": [0] * 512, "upper_leaf_after": [0] * 512,
            "lower_leaf_before": lower, "lower_leaf_after": final_lower,
            "recovery": {"capture_pc": capture, "guest_before": oracle.guest_seed(scenario),
                "guest_after": oracle.guest_after(scenario, completed), "control": [0x1000, 0, 1, 0, 0],
                "faults": len(faults), "walks": 0, "retries": 0 if scenario in ("device", "hole") else len(faults),
                "vector": 13 if faulted else 0, "error": 0, "linear": 0,
                "rip": oracle.EIP + oracle.GUEST_OFFSETS[completed] if faulted else 0,
                "tables_before": [0xA4] * 16384, "tables_after": [0xA4] * 16384,
                "memory_after": oracle.data_pages(scenario, completed)[0][:256], "bank_after": list(bank), "traces": traces}}


def change(path, value):
    def mutate(record):
        target = record
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return mutate


class OffStepsTests(unittest.TestCase):
    def test_all_ten_profiles_and_twenty_five_rejection_boundaries(self):
        for scenario in oracle.SCENARIOS:
            with self.subTest(scenario=scenario):
                oracle.check(synthetic(scenario), scenario)
        for mutation in range(25):
            with self.subTest(mutation=mutation):
                oracle.check(synthetic(mutation=mutation))

    def test_segment_service_a20_prefix_and_publication_corruptions_fail(self):
        mutations = [
            ("wrong offset versus linear", "segments", change(["steps", "data_linear", 2], oracle.DATA)),
            ("A20 modulo instead of bit20", "a20-off", change(["steps", "data_gpa", 2], 0)),
            ("A20 source not masked", "a20-off", change(["steps", "fetch_gpa", 0], 0x302000)),
            ("wrong physical backing", "a20-off", change(["lower_leaf_after", 256], 0)),
            ("A20 wrote wrong page", "a20-off", change(["data_pages_after", 0, 0], 0x42)),
            ("linear wrap omitted", "linear-wrap", change(["steps", "data_linear", 2], 0x100000000)),
            ("zero GPA confused with absent route", "linear-wrap", change(["steps", "entries", 2], 0)),
            ("DS fault consumed instruction", "ds-limit", change(["completed"], 3)),
            ("DS fault wrote memory", "ds-limit", change(["data_pages_after", 0, 0], 0x42)),
            ("DS fault changed CR2", "ds-limit", change(["recovery", "guest_after", "control", 1], 0x300000)),
            ("DS selector encoded as error", "ds-limit", change(["recovery", "error"], 0x10)),
            ("DS permission treated as PF", "ds-readonly", change(["recovery", "vector"], 14)),
            ("source middle fault used next RIP", "cs-inside-load", change(["recovery", "guest_after", "rip"], 0x2016)),
            ("source fault discarded earlier write", "cs-before-load", change(["data_pages_after", 0, 0], 0xA5)),
            ("first fetch invented compiler call", "cs-first", change(["requests"], 1)),
            ("first fetch fabricated execution", "cs-first", change(["steps", "entries", 0], 1)),
            ("native load has whole-block temporary", "segments", change(["final_frame", "x", 21], 0x42)),
            ("native partial load merge wrong", "segments", change(["steps", "prefixes", 7, "x", 4], 0x42)),
            ("native halfword fault already committed", "segments", change(["recovery", "traces", 2, "frame", "x", 20], 0x42)),
            ("native first store already wrote", "segments", change(["steps", "data_before_retry", 0, 0], 0x42)),
            ("lost prior store before load retry", "segments", change(["steps", "data_before_retry", 1, 0], 0xA5)),
            ("unrelated native flags", "segments", change(["final_frame", "nzcv"], 0)),
            ("unrelated canonical flags", "segments", change(["recovery", "guest_after", "rflags"], 2)),
            ("unrelated vector", "segments", change(["recovery", "guest_after", "ymm", 15, 31], 0)),
            ("device direct mapping", "device", change(["lower_leaf_after", 256], 1)),
            ("device retried native instruction", "device", change(["recovery", "retries"], 3)),
            ("device read value wrong", "device", change(["steps", "services", 1, 5], 0x12345678)),
            ("device order wrong", "device", change(["steps", "services", 0, 0], 4)),
            ("device wrote RAM", "device", change(["data_pages_after", 0, 0], 0x42)),
            ("hole retains store", "hole", change(["steps", "services", 1, 5], 0x42)),
            ("hole halfword width wrong", "hole", change(["steps", "services", 2, 5], 0xFFFFFFFF)),
            ("hole native merge skipped", "hole", change(["final_frame", "x", 4], 0xFFFF)),
            ("page walker used", "segments", change(["recovery", "walks"], 1)),
            ("table A bit changed", "segments", change(["recovery", "tables_after", 0], 0xA6)),
            ("source modified", "segments", change(["source_pages_after", 1, 4095], 0)),
            ("unowned data write", "segments", change(["data_pages_after", 2, 4095], 0)),
            ("body published in wrong slot", "segments", change(["code_after", 31], 1)),
            ("wrong service continuation", "segments", change(["service_pc"], oracle.PRIVATE + 0x41000)),
        ]
        for name, scenario, mutate in mutations:
            with self.subTest(corruption=name):
                record = synthetic(scenario)
                mutate(record)
                with self.assertRaises(ValueError):
                    oracle.check(record)
        for step in range(9):
            for word in range(16):
                record = synthetic()
                record["steps"]["replies"][step][word] ^= 1
                with self.subTest(step=step, word=word), self.assertRaises(ValueError):
                    oracle.check(record)
        for mutation in range(25):
            completed = oracle.mutation_site(mutation)[0]
            record = synthetic(mutation=mutation)
            record["code_after"][completed * 32] = 1
            with self.subTest(early_publish=mutation), self.assertRaises(ValueError):
                oracle.check(record)
        record = synthetic(mutation=0)
        record["data_pages_after"][0][0] = 0xA5
        with self.assertRaises(ValueError):
            oracle.check(record)

    def test_literal_routing_limits_and_instruction_bodies(self):
        self.assertEqual(oracle.profile("segments")["gpa"], 0x300000)
        self.assertEqual(oracle.profile("a20-off")["gpa"], 0x200000)
        self.assertEqual(oracle.profile("a20-off")["source_gpa"], 0x202000)
        self.assertEqual(oracle.profile("linear-wrap")["gpa"], 0)
        self.assertEqual(oracle.profile("ds-limit")["completed"], 2)
        self.assertEqual(oracle.profile("cs-before-load")["completed"], 4)
        self.assertEqual(oracle.profile("cs-inside-load")["completed"], 4)
        self.assertEqual(oracle.profile("cs-first")["completed"], 0)
        self.assertEqual(oracle.GUEST_OFFSETS[4], 17)
        for step in (2, 4, 7):
            self.assertEqual(oracle.BODIES[step][4:8].hex(), {2:"a4fc9f88",4:"84fedf88",7:"94fedf48"}[step])

    def test_single_instruction_native_effects_and_hole_values(self):
        initial, _ = oracle.native_seed()
        for hole in (False, True):
            frame = copy.deepcopy(initial)
            for step in range(9):
                previous = copy.deepcopy(frame)
                frame = oracle.native_step(frame, step, oracle.CODE, oracle.PRIVATE + 0xA0080, hole)
                if step in (2, 4, 7):
                    fault = oracle.native_step(previous, step, oracle.CODE, oracle.PRIVATE + 0xA0080, hole, fault=True)
                    self.assertEqual(fault["x"][4], previous["x"][4])
                    self.assertEqual(fault["x"][5], previous["x"][5])
                    if step != 2:
                        self.assertEqual(fault["x"][20], 0x100000)
            self.assertEqual(frame["x"][4], 0xFFFFFFFF if hole else 0xFFFF0042)
            self.assertEqual(frame["x"][7], frame["x"][4])
            self.assertEqual(frame["x"][5], 0xFFFFFFFF if hole else 0x42)
            self.assertEqual(frame["x"][20], 0xFFFF if hole else 0x42)
            self.assertEqual(frame["x"][21:23], initial["x"][21:23])
            self.assertEqual(frame["q"], initial["q"])
            self.assertEqual(frame["nzcv"], initial["nzcv"])

    def test_only_active_a20_source_page_contains_the_program(self):
        for scenario, active in (("segments", 0), ("a20-off", 1)):
            record = synthetic(scenario)
            oracle.check(record)
            pages = record["source_pages_before"]
            self.assertEqual(pages[active][:37], list(oracle.SOURCE_BYTES))
            self.assertEqual(pages[1 - active], [0x33 if active == 0 else 0xCC] * 4096)
            for mutation in ("duplicate", "swapped", "wrong_request_route"):
                bad = synthetic(scenario)
                if mutation == "duplicate":
                    for field in ("source_pages_before", "source_pages_after"):
                        bad[field][1 - active][:37] = oracle.SOURCE_BYTES
                elif mutation == "swapped":
                    for field in ("source_pages_before", "source_pages_after"):
                        bad[field] = list(reversed(bad[field]))
                else:
                    bad["steps"]["fetch_gpa"][0] = 0x302000 if active else 0x202000
                with self.subTest(scenario=scenario, mutation=mutation), self.assertRaises(ValueError):
                    oracle.check(bad)

    def test_precise_prefixes_and_distinct_backing(self):
        for scenario, completed, eax, ecx, edx in (
            ("ds-limit", 2, 0x42, 1, 0x100000),
            ("ds-readonly", 2, 0x42, 1, 0x100000),
            ("cs-before-load", 4, 0xFFFFFFFF, 1, 0x100000),
            ("cs-inside-load", 4, 0xFFFFFFFF, 1, 0x100000),
            ("segments", 9, 0xFFFF0042, 0xFFFF0042, 0x42),
            ("device", 9, 0xFFFF0042, 0xFFFF0042, 0x42),
            ("hole", 9, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF),
        ):
            guest = oracle.guest_after(scenario, completed)
            self.assertEqual(guest["gpr"][:3], [eax, ecx, edx])
            self.assertEqual(guest["rip"], 0x2000 + oracle.GUEST_OFFSETS[completed])
            self.assertEqual(guest["control"][1], 0xDEAD1000)
            self.assertEqual(guest["rflags"], 0x202)
        for scenario, selected in (("segments", 0), ("a20-off", 1), ("linear-wrap", 2)):
            pages = oracle.data_pages(scenario, 9)
            for page, fill in enumerate((0xA5, 0x5A, 0x3C)):
                self.assertEqual(pages[page], ([0x42, 0, 0, 0] if page == selected else [fill] * 4) + [fill] * 4092)
        self.assertEqual(oracle.data_pages("device", 9), oracle.data_pages("device", 0))
        self.assertEqual(oracle.data_pages("hole", 9), oracle.data_pages("hole", 0))

    def test_source_requests_distinguish_eip_linear_and_gpa(self):
        for scenario in oracle.SCENARIOS:
            for step in range(9):
                request = oracle.request_packet(37, scenario, step)
                self.assertEqual(request[7], 0x2000 + oracle.GUEST_OFFSETS[step])
                self.assertEqual(request[11], 0x300000 + request[7])
                self.assertEqual(request[12], request[11] - (0x100000 if scenario == "a20-off" else 0))
                self.assertEqual(request[6], oracle.SCENARIOS.index(scenario) + 1)
                source = request[9].to_bytes(8, "little")[:request[10]]
                self.assertEqual(source, oracle.SOURCE_BYTES[oracle.GUEST_OFFSETS[step]:oracle.GUEST_OFFSETS[step + 1]])
                reply = oracle.reply_packet(37, scenario, step)
                body = b"".join(word.to_bytes(8, "little") for word in reply[9:11])
                self.assertEqual(body, oracle.BODIES[step].ljust(16, b"\0"))


if __name__ == "__main__":
    unittest.main()

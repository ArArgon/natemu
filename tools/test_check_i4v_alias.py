import copy
import unittest

import check_i4
import check_i4_pressure
import check_i4v_alias as oracle


def fixture(n=4, profile=2):
    """Lift a valid independent I4 schedule into the declared alias phases."""
    record = check_i4_pressure.known_result(n)
    record.update(alias_profile=profile, source_rip=oracle.SOURCE + (oracle.HIGH if profile == 2 else 0))
    record["profile"] = "i4-alias-low" if profile == 1 else "i4-alias-high"

    def frame_addresses(frame, cpu, phase):
        frame["x"][6], frame["x"][11] = oracle.address(cpu, phase), oracle.address(cpu, phase, 128)

    def cell_addresses(cell, phase):
        cpu = cell["id"]
        for key in ("initial", "captured"):
            frame_addresses(cell[key], cpu, phase)
        cell["memory_base"] = oracle.address(cpu, phase)
        if "dirty_fault" in cell:
            frame_addresses(cell["dirty_fault"]["frame"], cpu, phase)
            cell["dirty_fault"]["far"] = oracle.address(cpu, phase, 128)

    for name, phase in (("cells", 0), ("new_cells", 1)):
        for cell in record[name]:
            cell_addresses(cell, phase)
    for fault in record["faults"]:
        frame_addresses(fault["frame"], fault["id"], 0)
        fault["far"] = oracle.address(fault["id"], 0)
    counts = [0] * n
    for cell in sorted(record["maintenance"]["intervals"], key=lambda item: item["ordinal"]):
        cpu = cell["id"]
        cell_addresses(cell, counts[cpu] + 2)
        counts[cpu] += 1
    for event in record["events"]:
        cpu = event["id"]
        phase = 0 if cpu in (1, 3) else 1
        event["packet"]["gpr"][3] = oracle.address(cpu, phase)
        event["packet"]["gpr"][7] = oracle.address(cpu, phase, 128)
        event["packet"]["rip"] = record["source_rip"] + (0 if phase == 0 else 17)
    for request in record["requests"]:
        request[12] = record["source_rip"]
        request[10] = 3
    record["aliases"] = oracle.expected_aliases(record, n)
    for item in record["aliases"]:
        item.update(sctlr=0x30D01985, actlr=2)
    return record


class AliasNormalizationTests(unittest.TestCase):
    def test_all_participant_counts_and_source_regimes(self):
        for n in range(1, 5):
            for profile in (1, 2):
                with self.subTest(n=n, profile=profile):
                    original = fixture(n, profile)
                    snapshot = copy.deepcopy(original)
                    normalized = oracle.normalize(original)
                    check_i4.check(normalized, n, 50, "i4-pressure")
                    oracle.check(original, n, 50, profile)
                    self.assertEqual(original, snapshot)

    def test_only_declared_address_fields_normalized(self):
        record = fixture()
        normalized = oracle.normalize(record)
        baseline = check_i4_pressure.known_result(4)
        baseline.update(alias_profile=record["alias_profile"], source_rip=record["source_rip"])
        baseline["aliases"] = record["aliases"]
        self.assertEqual(normalized, baseline)

    def test_address_errors_cannot_be_hidden_by_normalization(self):
        mutations = [
            ("truncated high load root", lambda r: r["cells"][0]["initial"]["x"].__setitem__(6, oracle.DATA)),
            ("wrong owner offset", lambda r: r["cells"][0]["initial"]["x"].__setitem__(6, oracle.HIGH + oracle.DATA + 256)),
            ("private pointer", lambda r: r["cells"][0]["initial"]["x"].__setitem__(6, oracle.PRIVATE)),
            ("wrong store alias", lambda r: r["cells"][0]["captured"]["x"].__setitem__(11, oracle.DATA + 128)),
            ("old phase reused after remap", lambda r: r["new_cells"][0]["initial"]["x"].__setitem__(6, oracle.HIGH + oracle.DATA)),
            ("fabricated low native FAR", lambda r: r["faults"][0].__setitem__("far", oracle.DATA)),
            ("wrong ordinary phase", lambda r: r["maintenance"]["intervals"][0]["initial"]["x"].__setitem__(6, oracle.DATA)),
            ("wrong ordinary memory base", lambda r: r["maintenance"]["intervals"][0].__setitem__("memory_base", oracle.DATA)),
            ("wrong ordinary store FAR", lambda r: r["maintenance"]["intervals"][0]["dirty_fault"].__setitem__("far", oracle.DATA + 128)),
            ("wrong canonical event RIP", lambda r: r["events"][0]["packet"].__setitem__("rip", oracle.SOURCE + 17)),
            ("wrong canonical event address", lambda r: r["events"][0]["packet"]["gpr"].__setitem__(3, oracle.HIGH + oracle.DATA)),
            ("wrong compile RIP", lambda r: r["requests"][2].__setitem__(12, oracle.SOURCE)),
            ("legacy reply profile", lambda r: r["requests"][0].__setitem__(10, 1)),
            ("missing ordinary interval", lambda r: r["maintenance"]["intervals"].pop(0)),
        ]
        for name, mutate in mutations:
            with self.subTest(mutation=name):
                record = fixture()
                mutate(record)
                with self.assertRaises(ValueError):
                    oracle.normalize(record)

    def test_unrelated_state_and_physical_addresses_are_not_erased(self):
        mutations = [
            ("physical GPA", lambda r: r["faults"][0].__setitem__("old_gpa", oracle.HIGH + oracle.DATA)),
            ("ordinary physical route", lambda r: r["maintenance"]["intervals"][0].__setitem__("gpa", oracle.HIGH + oracle.DATA)),
            ("native PC", lambda r: r["cells"][0].__setitem__("object_pc", oracle.HIGH + oracle.DATA)),
            ("unrelated native root", lambda r: r["new_cells"][0]["initial"]["x"].__setitem__(19, oracle.HIGH + oracle.DATA)),
            ("arithmetic result", lambda r: r["cells"][0]["captured"]["x"].__setitem__(4, oracle.HIGH + oracle.DATA)),
            ("canonical unrelated GPR", lambda r: r["events"][0]["packet"]["gpr"].__setitem__(15, oracle.HIGH + oracle.DATA)),
            ("source contents", lambda r: r["source"]["bytes"].__setitem__(2, 0xEF)),
        ]
        for name, mutate in mutations:
            with self.subTest(mutation=name):
                record = fixture()
                mutate(record)
                normalized = oracle.normalize(record)
                with self.assertRaises(ValueError):
                    check_i4.check(normalized, 4, 50, "i4-pressure")

    def test_both_aliases_must_be_acknowledged_before_reuse(self):
        mutations = [
            ("inactive alias never warmed", lambda r: r["aliases"][0]["warm"].__setitem__(0, 0)),
            ("active alias already mapped", lambda r: r["aliases"][0]["warm"].__setitem__(1, oracle.descriptor(0))),
            ("warm alias wrong old page", lambda r: r["aliases"][1]["warm"].__setitem__(1, oracle.descriptor(1))),
            ("missing acknowledgement", lambda r: r["aliases"][0].__setitem__("ack_count", 36)),
            ("stale low route acknowledged", lambda r: r["aliases"][0]["acks"][0].__setitem__(1, oracle.descriptor(0))),
            ("stale high route acknowledged", lambda r: r["aliases"][0]["acks"][0].__setitem__(2, oracle.descriptor(0))),
            ("wrong acknowledgement epoch", lambda r: r["aliases"][0]["acks"][1].__setitem__(0, 2)),
            ("writable route before dirty repair", lambda r: r["aliases"][0]["acks"][0].__setitem__(2, oracle.descriptor(1, False))),
            ("foreign CPU private root", lambda r: r["aliases"][1].__setitem__("ttbr1", oracle.IPA + 0x17000)),
            ("foreign CPU upper root link", lambda r: r["aliases"][1]["upper_root"].__setitem__(256, oracle.IPA + 0x18000 + 3)),
            ("extra upper route", lambda r: r["aliases"][1]["upper_leaf"].__setitem__(255, oracle.descriptor(0))),
            ("unreclaimed low route", lambda r: r["aliases"][1]["lower_leaf"].__setitem__(256, 0)),
            ("zero wrong RIP control", lambda r: r["aliases"][0].__setitem__("wrong_rip_rejections", 0)),
            ("early publication", lambda r: r["aliases"][0].__setitem__("blank_before_publish", 0)),
            ("duplicate alias owner", lambda r: r["aliases"][1].__setitem__("id", 0)),
        ]
        for name, mutate in mutations:
            with self.subTest(mutation=name):
                record = fixture()
                mutate(record)
                with self.assertRaises(ValueError):
                    oracle.check(record, 4, 50, 2)

    def test_source_write_must_use_other_alias_and_preserve_identity(self):
        for profile in (1, 2):
            mutations = [
                ("same source alias", lambda r: r["aliases"][0].__setitem__("source_address", r["source_rip"])),
                ("VA used as GPA", lambda r: r["aliases"][0].__setitem__("source_gpa", oracle.HIGH + oracle.SOURCE)),
                ("source lacks dirty", lambda r: r["aliases"][0].__setitem__("source_leaf_after", oracle.SOURCE | 0x27)),
                ("incorrect original source", lambda r: r["aliases"][0]["source_before"].__setitem__(2, 0xEB)),
                ("source mutation missing", lambda r: r["aliases"][0]["source_after"].__setitem__(2, 0xEF)),
                ("unowned source mutation", lambda r: r["aliases"][1].__setitem__("source_gpa", oracle.SOURCE)),
                ("source native route missing", lambda r: r["aliases"][0]["upper_leaf" if profile == 1 else "lower_leaf"].__setitem__(258, 0)),
            ]
            for name, mutate in mutations:
                with self.subTest(profile=profile, mutation=name):
                    record = fixture(profile=profile)
                    mutate(record)
                    with self.assertRaises(ValueError):
                        oracle.check(record, 4, 50, profile)
        with self.assertRaises(ValueError):
            oracle.check(fixture(profile=1), 4, 50, 2)


if __name__ == "__main__":
    unittest.main()

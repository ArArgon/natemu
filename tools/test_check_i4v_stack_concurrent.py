import copy
import unittest

import check_i4_pressure
import check_i4v_alias
import check_i4v_stack_concurrent as oracle


def native_fault(frame, far, pc, permission):
    return {"frame": frame, "far": far, "pc": pc, "esr": (0x25 << 26) | 64 | (15 if permission else 7),
            "spsr": frame["nzcv"] | 0x3C4}


def program(cpu, initial, before, replacement, capture, stack_profile=1):
    code = oracle.CODE_NEXT if replacement else oracle.CODE
    prefixes, after = oracle.execute(initial, before, cpu, code, capture, replacement, stack_profile)
    _, bank, _ = oracle.seed(cpu, stack_profile)
    cell = {"id": cpu, "case": "fex_push_immediates64_changed" if replacement else "fex_push_immediates64",
            "status": 1, "initial": copy.deepcopy(initial), "captured": copy.deepcopy(prefixes[-1]),
            "memory_base": oracle.span(cpu, stack_profile), "memory_before": list(before), "memory_after": after,
            "bank_before": bank, "bank_after": list(bank), "object_pc": code, "capture_pc": capture}
    if replacement:
        cell["dirty_faults"] = 1
        cell["dirty_fault"] = native_fault(oracle.precommit(initial, prefixes, 0, code, capture, True),
                                           initial["x"][8] - 8, code + 8, True)
    return cell, prefixes


def fixture(n=4, profile=2, stack_profile=1, actor=None):
    # This alternative legal FIFO schedule comes from the existing independent
    # reference. Its old scalar guest effects are replaced by real stack effects.
    record = check_i4_pressure.known_result(n)
    scheduled = actor is not None
    actor = 0 if actor is None else actor
    if scheduled:
        record.update(actor=actor, delayed_owner=(actor+n-1)%n)
        # Rotate logical protocol roles, leaving physical memory owners and
        # round-robin maintenance identities unchanged.
        start = next(i for i,w in enumerate(record['trace']) if w>>8>=23)
        record['trace'][:start] = [(w & ~255) | (0 if w>>8==3 else ((w&255)+actor)%n)
                                  for w in record['trace'][:start]]
        for event in record['events']:
            event['id']=(event['id']+actor)%n
            event['vector']=64+event['id']
        record['events'].sort(key=lambda e:e['id'])
    rip = oracle.SOURCE + (oracle.HIGH if profile == 2 else 0)
    name = {1: "stack", 2: "stack-parent", 3: "stack-leaf"}[stack_profile]
    record.update(profile="i4-" + name + ("-low" if profile == 1 else "-high"), alias_profile=profile,
                  stack_profile=stack_profile, source_rip=rip)
    capture, vm = oracle.PRIVATE + 0x400, 0x123456789ABCDEF
    frames, memory, programs, second_frames = {}, {}, {}, {}
    for cpu in range(n):
        initial, _, before = oracle.seed(cpu, stack_profile)
        old, prefixes = program(cpu, initial, before, False, capture, stack_profile)
        record["cells"][cpu] = old
        memory[(0, cpu)] = old["memory_after"]
        fault = native_fault(oracle.precommit(initial, prefixes, 1, oracle.CODE, capture, False),
                             initial["x"][8] - 10, oracle.CODE + 68, False)
        fault.update(id=cpu, faults=1, old_gpa=oracle.DATA + cpu * 256)
        record["faults"][cpu] = fault
        programs[cpu] = [{"entries": 31, "completed": 31, "prefixes": oracle.compact_prefixes(prefixes)}]
        initial = copy.deepcopy(prefixes[-1])
        initial["x"][16] = oracle.CODE_NEXT
        new, prefixes = program(cpu, initial, before, True, capture, stack_profile)
        record["new_cells"][cpu] = new
        memory[(1, cpu)] = new["memory_after"]
        programs[cpu].append({"entries": 31, "completed": 31, "prefixes": oracle.compact_prefixes(prefixes)})
        second_frames[cpu] = copy.deepcopy(prefixes[-1])
        frames[cpu] = copy.deepcopy(prefixes[-1])
    intervals = []
    for original in record["maintenance"]["intervals"]:
        cpu, page = original["id"], int(original["heads"] % 2 == 0)
        initial = copy.deepcopy(frames[cpu])
        initial["x"][16] = oracle.CODE_NEXT
        cell, prefixes = program(cpu, initial, memory[(page, cpu)], True, capture, stack_profile)
        cell.update({key: original[key] for key in ("ordinal", "heads", "epoch", "gpa")})
        intervals.append(cell)
        frames[cpu], memory[(page, cpu)] = copy.deepcopy(prefixes[-1]), cell["memory_after"]
        programs[cpu].append({"entries": 31, "completed": 31, "prefixes": oracle.compact_prefixes(prefixes)})
    record["maintenance"]["intervals"] = intervals
    for event in record["events"]:
        cpu = event["id"]
        event["packet"] = oracle.canonical_seed(cpu, rip, stack_profile, actor) if (cpu+n-actor)%n in (1, 3) else oracle.canonical_after(cpu, rip, second_frames[cpu], stack_profile=stack_profile, actor=actor)
    requests = [oracle.request_packet(vm, rip, serial, owner=actor) for serial in (1, 2, 3)]
    replies = [[oracle.reply_packet(vm, rip, serial, chunk, actor) for chunk in range(3)] for serial in (1, 2, 3)]
    record["requests"] = copy.deepcopy(requests)
    record["source"].update(bytes=list(oracle.NEW_SOURCE), old_code=[0] * 320, new_code=oracle.code_image(True))
    record["aliases"] = check_i4v_alias.expected_aliases(record, n)
    if scheduled and actor:
        source_fields=('source_address','source_gpa','source_leaf_before','source_leaf_after',
                       'source_before','source_after')
        first,chosen=record['aliases'][0],record['aliases'][actor]
        for key in source_fields:first[key],chosen[key]=chosen[key],first[key]
        for side in ('lower_leaf','upper_leaf'):
            first[side][258],chosen[side][258]=chosen[side][258],first[side][258]
    page = int(record["maintenance"]["total"] % 2 == 0)
    for item in record["aliases"]:
        cpu = item["id"]
        warm = [oracle.descriptor(0), 0] if stack_profile == 1 and cpu % 2 == 0 else [0, oracle.descriptor(0)]
        item.update(sctlr=0x30D01985, actlr=2, warm=warm,
                    wrong_rip_rejections=0, blank_before_publish=0,
                    source_before=list(oracle.OLD_SOURCE[:17]) if cpu == actor else [0] * 17,
                    source_after=list(oracle.NEW_SOURCE[:17]) if cpu == actor else [0] * 17)
        item["lower_leaf"][256] = item["upper_leaf"][256] = oracle.descriptor(page)
    if stack_profile == 1:
        last_cpu = intervals[-1]["id"]
        record["aliases"][last_cpu]["upper_leaf" if last_cpu % 2 == 0 else "lower_leaf"][256] = oracle.descriptor(page, False)
    else:
        for item in record["aliases"]:
            item["lower_leaf"][256] = oracle.descriptor(page, False)
            if stack_profile == 3:
                item["upper_leaf"][256] = 0
                item["upper_leaf"][257] = oracle.descriptor(page)
            if stack_profile == 2 and item["id"] == 0:
                item["source_address"] = oracle.SOURCE
                item["upper_leaf"][258] = 0
                item["lower_leaf"][258] = oracle.descriptor(2, False)
        for cpu in reversed(range(n)):
            record["trace"].extend([32 << 8 | cpu, 33 << 8 | cpu])
    stacks = []
    for cpu in range(n):
        compiler = cpu == actor
        count = len(programs[cpu])
        stacks.append({"id": cpu, "step": 4, "pass": count - 1, "code": oracle.CODE_NEXT,
                       "expected": oracle.request_packet(vm, rip, 3, 2, actor) if compiler else [0] * 16,
                       "compile_requests": copy.deepcopy(requests) if compiler else [[0] * 16 for _ in range(3)],
                       "replies": copy.deepcopy(replies) if compiler else [[[0] * 16 for _ in range(3)] for _ in range(3)],
                       "programs": programs[cpu] + [{"entries": 0, "completed": 0, "prefixes": [[0] * 4 for _ in range(5)]} for _ in range(6 - count)],
                       "stale_chunks": 3 if compiler else 0, "wrong_owner_rejections": int(compiler),
                       "blank_before_publish": int(compiler),
                       "source_before": list(oracle.OLD_SOURCE) if compiler else [0] * 20,
                       "source_after": list(oracle.NEW_SOURCE) if compiler else [0] * 20,
                       "guest_before": oracle.canonical_seed(cpu, rip, stack_profile, actor), "guest_after": oracle.canonical_after(cpu, rip, frames[cpu], stack_profile=stack_profile, actor=actor)})
    for cpu, item in enumerate(stacks):
        probe = oracle.probe_expectation(cpu, rip, frames[cpu], stack_profile, page, capture)
        probe.update(esr=(0x25 << 26) | 64 | 15, spsr=frames[cpu]["nzcv"] | 0x3C4)
        item["probe"] = oracle.zeros(probe) if stack_profile == 1 else probe
        if stack_profile >= 2:
            item["guest_after"] = copy.deepcopy(probe["guest_after"])
    record["stacks"] = stacks
    record["stack_memory_before"] = [0xA5] * 8192
    physical = [0xA5] * 8192
    for (page, cpu), contents in memory.items():
        offset = page * 4096 + cpu * 256
        physical[offset:offset + 256] = contents
    record["stack_memory_after"] = physical
    return record


def change(path, value):
    def mutate(record):
        target = record
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return mutate


class ConcurrentStackTests(unittest.TestCase):
    def test_actual_actor_rotation_changes_protocol_and_owned_evidence(self):
        for n in range(1,5):
            for actor in range(n):
                for profile in (1,2):
                    with self.subTest(n=n,actor=actor,profile=profile):
                        r=fixture(n,profile,actor=actor)
                        oracle.check(r,n,profile,1,actor=actor)
                        self.assertEqual(r['requests'][0][3],actor)
                        self.assertEqual(next(w&255 for w in r['trace'] if w>>8==16),actor)
                        self.assertEqual(next(w&255 for w in r['trace'] if w>>8==7),(actor+n-1)%n)

    def test_rotation_cannot_be_satisfied_by_labels(self):
        original=fixture(4,1,actor=2)
        mutations={
            'actor label':lambda r:r.update(actor=0),
            'delayed label':lambda r:r.update(delayed_owner=3),
            'compiler identity':lambda r:r['requests'][0].__setitem__(3,0),
            'reply identity':lambda r:r['stacks'][2]['replies'][0][0].__setitem__(3,0),
            'compiler record owner':lambda r:r['stacks'][2].__setitem__('stale_chunks',0),
            'source writer':lambda r:r['aliases'][2].__setitem__('source_gpa',0),
            'IF owner':lambda r:r['stacks'][2]['guest_before'].__setitem__('rflags',0x202),
            'event wait owner':lambda r:r['events'][2].__setitem__('wait_checks',0),
            'shadow role':lambda r:r['events'][3].__setitem__('blocked',2|16|32|64),
        }
        for kind in (7,8,16,22):
            mutations['trace role '+str(kind)]=lambda r,kind=kind:r['trace'].__setitem__(
                next(i for i,w in enumerate(r['trace']) if w>>8==kind),kind<<8)
        for name,change in mutations.items():
            with self.subTest(name=name):
                r=copy.deepcopy(original);change(r)
                with self.assertRaises(ValueError):oracle.check(r,4,1,1,actor=2)
        with self.assertRaises(ValueError):oracle.check(original,4,1,1)
        r=fixture(4,1);r.update(actor=2,delayed_owner=1)
        with self.assertRaises(ValueError):oracle.check(r,4,1,1,actor=2)

    def test_all_counts_and_regimes(self):
        for n in range(1, 5):
            for profile in (1, 2):
                for stack_profile in (1, 2, 3):
                    with self.subTest(n=n, profile=profile, stack_profile=stack_profile):
                        oracle.check(fixture(n, profile, stack_profile), n, profile, stack_profile)

    def test_literal_old_and_replacement_vectors(self):
        for cpu in range(4):
            initial, _, before = oracle.seed(cpu)
            s = initial["x"][8]
            old, memory = oracle.execute(initial, before, cpu, oracle.CODE, oracle.PRIVATE + 0x400, False)
            self.assertEqual(old[0]["x"][8], s - 8)
            self.assertEqual(old[-1]["x"][8], s - 12)
            self.assertEqual(old[-1]["x"][4], 0xFFFFFFFFBEBDBCBB)
            self.assertEqual(old[-1]["x"][6], 0x51526162)
            self.assertEqual(bytes(memory[116:128]).hex(), "62615251bbbcbdbeffffffff")
            new, memory = oracle.execute(old[-1], before, cpu, oracle.CODE_NEXT, oracle.PRIVATE + 0x400, True)
            self.assertEqual(new[-1]["x"][8], s - 24)
            self.assertEqual(new[-1]["x"][4], 0xA5A5A5A5A5A5A5A5)
            self.assertEqual(new[-1]["x"][6], 0xA5A5A5A5)
            self.assertEqual(bytes(memory[104:116]).hex(), "62615351bbbcbdbeffffffff")
            self.assertEqual(memory[:104] + memory[116:], [0xA5] * 244)

    def test_six_programs_stay_in_span_and_never_reset_rsp(self):
        for cpu in range(4):
            frame, _, before = oracle.seed(cpu)
            s = frame["x"][8]
            memory = list(before)
            for count in range(1, 7):
                prefixes, memory = oracle.execute(frame, memory, cpu, oracle.CODE_NEXT, oracle.PRIVATE + 0x400, count > 1)
                frame = prefixes[-1]
                self.assertEqual(frame["x"][8], s - 12 * count)
            self.assertEqual(frame["x"][8] - oracle.span(cpu), 56)
            self.assertEqual(memory[:56], [0xA5] * 56)
            self.assertEqual(memory[128:], [0xA5] * 128)

    def test_corrupt_state_faults_and_memory_fail(self):
        mutations = [
            ("first push prefix missing", change(["stacks", 0, "programs", 0, "prefixes", 0, 0], oracle.span(0) + 128)),
            ("failed preindex committed RSP", change(["faults", 0, "frame", "x", 8], oracle.span(0) + 118)),
            ("failed instruction scratch absent", change(["faults", 0, "frame", "x", 20], 0)),
            ("fault moved to first push", change(["faults", 0, "pc"], oracle.CODE + 8)),
            ("lost retained physical route", change(["faults", 0, "old_gpa"], oracle.DATA + 4096)),
            ("reset RSP after remap", change(["new_cells", 0, "initial", "x", 8], oracle.span(0) + 128)),
            ("reinterpreted RBX as address", change(["new_cells", 0, "initial", "x", 6], oracle.span(0))),
            ("RDX changed with RSP", change(["new_cells", 0, "initial", "x", 5], oracle.span(0) + 116)),
            ("stale replacement source", change(["new_cells", 0, "memory_after", 106], 0x52)),
            ("wrong replacement physical load", change(["new_cells", 0, "captured", "x", 4], 0xFFFFFFFFBEBDBCBB)),
            ("wrong native code slot", change(["new_cells", 0, "captured", "x", 16], oracle.CODE + 256)),
            ("unrelated native register", change(["new_cells", 0, "captured", "x", 19], 0)),
            ("native SP confused with RSP", change(["cells", 0, "captured", "sp"], oracle.span(0))),
            ("ordinary RSP reset", change(["maintenance", "intervals", 0, "initial", "x", 8], oracle.span(0) + 116)),
            ("ordinary byte reseeding", change(["maintenance", "intervals", 0, "memory_before", 0], 0)),
            ("ordinary native fault lost", change(["maintenance", "intervals", 0, "dirty_faults"], 0)),
            ("ordinary dirty fault RSP committed", change(["maintenance", "intervals", 0, "dirty_fault", "frame", "x", 8], oracle.span(0) + 96)),
            ("event wrong committed RSP", change(["events", 0, "packet", "gpr", 4], oracle.span(0) + 116)),
            ("event old scalar flags", change(["events", 0, "packet", "rflags"], 0x246)),
            ("canonical vector loss", change(["stacks", 0, "guest_after", "ymm", 15, 31], 0)),
            ("canonical private register loss", change(["stacks", 0, "guest_after", "gpr", 15], 0)),
            ("canonical flags changed", change(["stacks", 0, "guest_after", "rflags"], 0x246)),
            ("missing guest instruction", change(["stacks", 0, "programs", 0, "completed"], 15)),
            ("boolean bitmask", change(["stacks", 0, "programs", 0, "entries"], True)),
            ("unused prefix populated", change(["stacks", 3, "programs", 5, "prefixes", 0, 0], 1)),
            ("wrong final high alias", change(["aliases", 0, "upper_leaf", 256], 0)),
            ("active high alias prematurely warm", change(["aliases", 0, "warm", 1], oracle.descriptor(0))),
            ("inactive low alias not warm", change(["aliases", 0, "warm", 0], 0)),
            ("active low alias prematurely warm", change(["aliases", 1, "warm", 0], oracle.descriptor(0))),
            ("inactive high alias not warm", change(["aliases", 1, "warm", 1], 0)),
            ("native alignment checking enabled", lambda r: r["aliases"][0].__setitem__("sctlr", r["aliases"][0]["sctlr"] | 2)),
            ("unowned physical write", change(["stack_memory_after", 4095], 0)),
            ("missing actual source mutation", change(["stacks", 0, "source_after", 7], 0x52)),
            ("old code not retired", change(["source", "old_code", 0], 0x94)),
            ("replacement return corrupted", change(["source", "new_code", 12], 0)),
            ("stale chunk accepted", change(["stacks", 0, "stale_chunks"], 2)),
            ("owner rejection absent", change(["stacks", 0, "wrong_owner_rejections"], 0)),
            ("early publication", change(["stacks", 0, "blank_before_publish"], 0)),
        ]
        for name, mutate in mutations:
            with self.subTest(corruption=name):
                record = fixture()
                mutate(record)
                with self.assertRaises(ValueError):
                    oracle.check(record, 4, 2)

    def test_mixed_alias_probe_literal_effects_and_overlapping_grants(self):
        for stack_profile in (2, 3):
            record = fixture(4, 1, stack_profile)
            record["trace"][-8:] = [32 << 8 | cpu for cpu in range(4)] + [33 << 8 | cpu for cpu in reversed(range(4))]
            oracle.check(record, 4, 1, stack_profile)
            for cpu, stack in enumerate(record["stacks"]):
                probe = stack["probe"]
                programs = sum(p["completed"] == 31 for p in stack["programs"])
                low_rsp = 0x100000 + cpu * 256 + 128 - 12 * programs
                high_rsp = 0xFFFF800000000000 + low_rsp + (4096 if stack_profile == 3 else 0)
                self.assertEqual(probe["guest_before"]["gpr"][4], low_rsp)
                self.assertEqual(probe["initial"]["x"][8], high_rsp)
                self.assertEqual(probe["captured"]["x"][8], high_rsp)
                self.assertEqual(probe["captured"]["x"][20], 0xFFFFFFFFBEBDBCBB)
                self.assertEqual(probe["guest_after"]["control"][1], high_rsp - 8)
                self.assertEqual(probe["guest_after"]["gpr"][2], 0x100000 + cpu * 256 + 128)
                self.assertEqual(probe["guest_after"]["rip"], 0x102000)
                self.assertEqual((probe["vector"], probe["error"]), (14, 3))
                self.assertEqual(probe["pair"][0] & 128, 0)
                self.assertEqual(probe["pair"][1] & 128, 128)
                self.assertEqual(probe["tables_after"][3] & 64, 0)

    def test_mixed_alias_fault_permission_and_prefix_corruptions_fail(self):
        for stack_profile in (2, 3):
            source = fixture(4, 2, stack_profile)
            probe = source["stacks"][0]["probe"]
            upper_index = 257 if stack_profile == 3 else 256
            mutations = [
                ("other alias inherited writable", change(["aliases", 0, "upper_leaf", upper_index], oracle.descriptor(1, False))),
                ("other alias wrong physical page", change(["aliases", 0, "upper_leaf", upper_index], oracle.descriptor(0))),
                ("low route lost dirty writability", change(["aliases", 0, "lower_leaf", 256], oracle.descriptor(1))),
                ("probe pair writable denial", change(["stacks", 0, "probe", "pair", 1], oracle.descriptor(1, False))),
                ("probe companion D set", lambda r: r["stacks"][0]["probe"]["tables_after"].__setitem__(3, r["stacks"][0]["probe"]["tables_after"][3] | 64)),
                ("probe permission bypass", change(["stacks", 0, "probe", "error"], 0)),
                ("probe exception absent", change(["stacks", 0, "probe", "vector"], 0)),
                ("probe fabricated native fault", change(["stacks", 0, "probe", "esr"], 0)),
                ("probe translation instead of permission", change(["stacks", 0, "probe", "esr"], (0x25 << 26) | 64 | 7)),
                ("probe wrong native site", change(["stacks", 0, "probe", "pc"], oracle.CODE_NEXT)),
                ("probe wrong FAR", change(["stacks", 0, "probe", "far"], oracle.DATA)),
                ("probe wrong CR2", change(["stacks", 0, "probe", "guest_after", "control", 1], oracle.DATA)),
                ("probe preindex wrote RSP", change(["stacks", 0, "probe", "captured", "x", 8], probe["address"])),
                ("probe preindex committed guest RSP", change(["stacks", 0, "probe", "guest_after", "gpr", 4], probe["address"])),
                ("probe scratch absent", change(["stacks", 0, "probe", "captured", "x", 20], 0)),
                ("probe RDX incorrectly rebound", change(["stacks", 0, "probe", "initial", "x", 5], probe["initial"]["x"][8])),
                ("probe lost committed prefix", change(["stacks", 0, "probe", "guest_before", "gpr", 4], oracle.DATA + 128)),
                ("probe advanced RIP", change(["stacks", 0, "probe", "guest_after", "rip"], source["source_rip"] + 5)),
                ("probe completed instruction bit", change(["stacks", 0, "programs", 5, "completed"], 1)),
                ("probe silently wrote physical byte", change(["stack_memory_after", 4100], 0)),
                ("probe lacked completion", change(["stacks", 0, "probe", "done"], 0)),
                ("probe completion without grant", change(["trace", -8], 33 << 8 | 3)),
                ("probe duplicate owner", change(["trace", -2], 32 << 8 | 1)),
                ("pressure resumed after terminal grant", change(["trace", -3], 26 << 8 | 1)),
            ]
            for name, mutate in mutations:
                with self.subTest(stack_profile=stack_profile, corruption=name):
                    record = copy.deepcopy(source)
                    mutate(record)
                    with self.assertRaises(ValueError):
                        oracle.check(record, 4, 2, stack_profile)
        record = fixture(1, 1, 1)
        record["stacks"][0]["probe"]["done"] = 1
        with self.assertRaises(ValueError):
            oracle.check(record)

    def test_chunk_identity_and_fifo_cannot_be_forged(self):
        for serial in range(3):
            for chunk in range(3):
                for word in (2, 3, 4, 5, 6, 7, 8, 13, 14, 15):
                    with self.subTest(serial=serial, chunk=chunk, word=word):
                        record = fixture(1)
                        record["stacks"][0]["replies"][serial][chunk][word] ^= 1
                        with self.assertRaises(ValueError):
                            oracle.check(record, 1, 2)
        for field in ("requests", "compile_requests"):
            record = fixture(1)
            target = record if field == "requests" else record["stacks"][0]
            target[field][2][7] = oracle.SOURCE
            with self.assertRaises(ValueError):
                oracle.check(record)
        for name, mutate in (("wrong FIFO owner", change(["maintenance", "request_history", 0, 0], 3)),
                             ("lost pending identity", change(["maintenance", "request_history", 0, 1], 99)),
                             ("missing pair acknowledgement", change(["aliases", 0, "acks", 1, 2], 0)),
                             ("missing ordinary progress", lambda r: r["maintenance"]["intervals"].pop())):
            with self.subTest(corruption=name):
                record = fixture()
                mutate(record)
                with self.assertRaises(ValueError):
                    oracle.check(record)


if __name__ == "__main__":
    unittest.main()

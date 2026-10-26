import copy
import unittest

import check_i4v_stack_boundary as oracle
from test_check_i4v_stack_concurrent import fixture as baseline_fixture


def fixture():
    frame = {"x": list(range(31)), "sp": 0x1234, "pstate": 0xA00003C4}
    boundary = oracle.HIGH + 0x101000
    frame["x"][8], frame["x"][5] = boundary + 4, boundary + 8
    memory = {0x100000 + i: (i * 37 + 19) % 256 for i in range(8192)}
    pages = {boundary - 4096: 0x101000, boundary: 0x100000}
    return frame, memory, pages, boundary


def wrapper_fixture(n=2, profile=1, actor=0):
    region = "high" if (profile + actor) % 2 else "low"
    record = baseline_fixture(n=n, profile=2 if region == "high" else 1)
    record["finished"] = 0
    shared = {"n": n, "profile": profile, "actor": actor,
              "data_before": copy.deepcopy(record["stack_memory_after"]),
              "tables_before": oracle.prefix_tables(n), "finished_before": 0,
              "trace_start": len(record["trace"]), "code_before": oracle.baseline.code_image(True)}
    frames = {c["id"]: c["captured"] for c in record["new_cells"]}
    for cell in sorted(record["maintenance"]["intervals"], key=lambda c: c["ordinal"]):
        frames[cell["id"]] = cell["captured"]
    owners = [{"id": cpu, "before_frame": frames[cpu], "before_guest": record["stacks"][cpu]["guest_after"],
               "lower_before": record["aliases"][cpu]["lower_leaf"],
               "upper_before": record["aliases"][cpu]["upper_leaf"]} for cpu in range(n)]
    return {"case": "stack-boundary", "profile": profile, "outcome": oracle.OUTCOMES[profile - 1],
            "region": region, "actor": actor, "n": n, "configured_vcpus": n, "tested_vcpus": n,
            "prefix": record, "boundary": shared, "owners": owners}


def schedule_fixture(n=3, profile=1, actor=1):
    delayed = (actor + n - 1) % n
    peers = [p for p in range(n) if p != actor and p != delayed]
    if n > 1:
        peers.append(delayed)
    order = [p for p in range(n) if p != delayed] + [delayed]
    events = [(40, 0)] + [(1, p) for p in range(n)]
    for p in peers:
        events += [(2, p), (42, p)]
    if profile >= 3:
        events.append((44 if profile == 3 else 45, actor))
    if profile != 3:
        events.append((43, actor))
    events.append((47, actor))
    if peers:
        events.append((3, 0))
        for p in peers:
            events += [(4, p), (46, p), (5, p)]
    events.append((48, actor))
    events += [(49, p) for p in order] + [(50, p) for p in order]
    events.append((51, delayed))
    events += [(52, p) for p in range(n)]
    trace = [(event << 8) | p for event, p in events]
    all_bits = (1 << n) - 1
    wave = {"participants": n, "scenario": 0, "alias_profile": 0, "stack_profile": 0,
            "mixed_profile": 0, "mixed_compiled": 0, "ready": all_bits, "finished": all_bits,
            "lock": 1000, "next_ticket": 1000, "phase": 2, "active": 0,
            "queued": all_bits ^ (1 << actor), "queue": peers + [oracle.MASK64] * (4 - len(peers)),
            "head": len(peers), "length": len(peers), "servicing": oracle.MASK64,
            "completed": all_bits, "retained": 0, "generation": 2, "acknowledgements": 0,
            "new_entries": 0, "new_completed": 0, "new_references": 0, "reclaimed": 1,
            "old_eligible": 0, "published": 1, "stale_replies": 0, "stale_entries": 0,
            "reuse_blocked": 0, "translation_wait": 0, "event_pending": [0] * 4,
            "event_notifications": [0] * 4, "trace_length": len(trace), "trace": trace}
    shared = {name: all_bits for name in ("ready", "done", "acknowledgements", "released",
                                         "reuse_blocked", "entry_rejected", "retained_before_release")}
    shared.update({name: 1 for name in ("setup", "actor_done", "remapped", "reclaimed")})
    shared["wave"] = wave
    return shared


def full_fixture(n=2, profile=1, actor=0):
    """Synthetic evidence, constructed before checking; never native execution."""
    record = wrapper_fixture(n, profile, actor)
    s = record["boundary"]
    s.update(schedule_fixture(n, profile, actor))
    p = oracle.DATA + (4096 if n % 2 == 0 else 0)
    q = oracle.DATA + 4096 - (p - oracle.DATA)
    s.update(old_page=p, other_page=q, code_at_outcome=oracle.baseline.code_image(True),
             configured_leaves=[q | 7 if profile == 1 else 0,
                                q | 7 if profile >= 3 else 0,
                                (0 if profile == 4 else p | (5 if profile == 5 else 7)) if profile >= 3 else 0])
    pages = {oracle.DATA: p, oracle.HIGH + oracle.DATA: p,
             oracle.HIGH: q, oracle.BOUNDARY - 4096: q, oracle.BOUNDARY: p}
    before = dict(enumerate(s["data_before"], oracle.DATA))
    after = dict(before)
    capture, code = oracle.PRIVATE + 0xA0080, oracle.baseline.CODE_NEXT
    rip = 0x102000 + (oracle.HIGH if record["region"] == "high" else 0)
    for cpu, o in enumerate(record["owners"]):
        initial = copy.deepcopy(o["before_frame"])
        if cpu == actor:
            if profile == 1:
                initial["x"][8] = initial["x"][5] = oracle.HIGH + 8
            elif profile == 2:
                initial["x"][5] = oracle.PRIVATE + 8
            else:
                initial["x"][8], initial["x"][5] = oracle.BOUNDARY + 4, oracle.BOUNDARY + 8
        initial["x"][16], initial["x"][30] = code, capture
        guest = copy.deepcopy(o["before_guest"])
        guest["gpr"][2], guest["gpr"][4], guest["rip"] = initial["x"][5], initial["x"][8], rip
        done = {1: 1, 2: 3, 3: 5, 4: 0, 5: 0}[profile] if cpu == actor else 5
        current, memory, prefixes = oracle.prefix(initial, before, pages, done, code, capture)
        for address in memory:
            if memory[address] != before[address]:
                after[address] = memory[address]
        split = cpu == actor and profile >= 3
        fault = split or cpu != actor
        fault_step = 0 if split else 1
        fault_initial = initial if split else prefixes[0] if fault else None
        fault_frame = oracle.native_fault_frame(fault_initial, fault_step, code, capture) if fault else oracle.zero_frame()
        if done == 0:
            current = copy.deepcopy(fault_frame)
        entered = copy.deepcopy(initial if done <= 1 else prefixes[done - 2])
        entered["x"][16], entered["x"][30] = code + max(0, done - 1) * 64, capture
        _, vector, error, address = oracle.outcome(oracle.SCENARIOS[profile - 1], oracle.BOUNDARY) if cpu == actor else (5, 0, 0, 0)
        final_guest = oracle.canonical_after(guest, current, rip, done, vector, address)
        o.update(initial=initial, initial_guest=guest, current=current,
            prefixes=prefixes + [oracle.zero_frame() for _ in range(5 - done)],
            entries=(1 << done) - 1 if done else 1, completed=(1 << done) - 1,
            step=min(done, 4), fault_frame=fault_frame, faults=int(fault), guest_after=final_guest,
            vector=vector, error=error, address=address, rip=rip + oracle.OFFSETS[done] if vector else 0,
            saved_page=q if cpu == actor and profile != 2 else p, saved_writable=1,
            native_pair_before=[0, 0] if split else [oracle.descriptor(((q if cpu == actor and profile == 1 else p) - oracle.DATA) // 4096, False), 0],
            native_pair_after=[oracle.descriptor((q - oracle.DATA) // 4096, False), oracle.descriptor((p - oracle.DATA) // 4096, False)] if split and profile == 3 else [0, 0],
            service_attempts=int(split), service_completed=int(split and profile == 3),
            service_pc=code + 8 if split else 0, service_resume_pc=code + 12 if split and profile == 3 else 0,
            ready_mask=(3 if profile == 3 else 1) if split else 0,
            retained_at_outcome=(1 << n) - 1 if cpu == actor else 0,
            ack_pair=[oracle.descriptor((q - oracle.DATA) // 4096)] * 2, released=1,
            final_frame=current, final_guest=final_guest, entered=entered, status=1, detail=0,
            capture_pc=capture, current_el=4, sctlr=0x30D01985, actlr=2, tcr=oracle.baseline.TCR,
            final_code=[0] * 320, bank_after=record["prefix"]["cells"][cpu]["bank_after"])
        o.update(esr=(0x25 << 26) | 64 | 7 if fault else 0,
                 far=oracle.access(fault_initial, fault_step)[0] if fault else 0,
                 pc=code + fault_step * 64 + (8 if split else 4) if fault else 0,
                 spsr=fault_frame["nzcv"] | 0x3C4 if fault else 0)
        for name, previous in (("final_lower_leaf", o["lower_before"]), ("final_upper_leaf", o["upper_before"])):
            leaves = list(previous)
            for i in (0, 256, 257, 260, 261):
                leaves[i] = 0
            leaves[256] = oracle.descriptor((q - oracle.DATA) // 4096)
            o[name] = leaves
        if split:
            prior = [before[q + 4092 + i] for i in range(4)] + [before[p + i] for i in range(4)]
            o.update(data_before_service=prior,
                     data_after_service=list(bytes.fromhex("bbbcbdbeffffffff")) if profile == 3 else prior,
                     dirty_before=s["configured_leaves"][1:],
                     dirty_after=[q | (0x67 if profile == 3 else 0x27), p | 0x67 if profile == 3 else s["configured_leaves"][2]])
            o["fragments"] = [{"linear": oracle.BOUNDARY - 4, "bytes": 4, "gpa": q + 4092,
                               "leaf": 0x4820, "before": q | 7, "after": q | 0x27, "ready": 1},
                              {"linear": oracle.BOUNDARY, "bytes": 4, "gpa": p if profile == 3 else 0,
                               "leaf": 0x4828, "before": s["configured_leaves"][2],
                               "after": p | 0x27 if profile == 3 else s["configured_leaves"][2], "ready": int(profile == 3)}]
        else:
            o.update(data_before_service=[0] * 8, data_after_service=[0] * 8,
                     dirty_before=[0, 0], dirty_after=[0, 0],
                     fragments=[dict.fromkeys(("linear", "bytes", "gpa", "leaf", "before", "after", "ready"), 0) for _ in range(2)])
    record["data_after"] = [after[oracle.DATA + i] for i in range(8192)]
    record["tables_after"] = list(s["tables_before"])
    oracle.table_word(record["tables_after"], 0x4800, q | 0x27)
    if profile == 1:
        oracle.table_word(record["tables_after"], 0x4000, p | 7)
    if profile >= 3:
        oracle.table_word(record["tables_after"], 0x4820, p | 7)
        oracle.table_word(record["tables_after"], 0x4828, q | 7)
    record["source_after"] = list(oracle.baseline.NEW_SOURCE)
    return record


class BoundaryTests(unittest.TestCase):
    def test_split_success_uses_saved_memory_and_changed_source(self):
        initial, before, pages, boundary = fixture()
        final, after, frames = oracle.prefix(initial, before, pages, 5, 0x30000, 0xA0080)
        self.assertEqual(final["x"][8], boundary - 8)
        tail = bytes(before[0x100000 + i] for i in range(4, 8))
        self.assertEqual(final["x"][4], int.from_bytes(b"\xff" * 4 + tail, "little"))
        self.assertEqual(final["x"][6], 0xBEBDBCBB)
        writes = oracle.physical_bytes(boundary - 8, 12, pages)
        self.assertEqual(bytes(after[p] for p in writes), bytes.fromhex("62615351bbbcbdbeffffffff"))
        self.assertTrue(all(after[p] == before[p] for p in before if p not in writes))
        self.assertEqual(len(frames), 5)
        self.assertEqual(initial["x"][8], boundary + 4)

    def test_late_faults_keep_exact_committed_prefix(self):
        initial, before, pages, boundary = fixture()
        for scenario, completed, vector in (("ss_after_first", 1, 12), ("gp_after_pushes", 3, 13)):
            frame, memory = copy.deepcopy(initial), dict(before)
            mapping = dict(pages)
            if vector == 12:
                frame["x"][8] = oracle.HIGH + 8
                mapping[oracle.HIGH] = 0x100000
            else:
                frame["x"][5] = oracle.PRIVATE + 8
            final, after, _ = oracle.prefix(frame, memory, mapping, completed, 0x30000, 0xA0080)
            address, width = oracle.access(final, completed)
            self.assertFalse(oracle.canonical_span(address, width))
            self.assertEqual(oracle.outcome(scenario, boundary), (completed, vector, 0, address))
            guest = {"gpr": list(range(16)), "control": [1, 0x87654321, 3, 4], "rip": 0, "rflags": 0x202}
            actual = oracle.canonical_after(guest, final, 0x102000, completed, vector, address)
            self.assertEqual(actual["control"], guest["control"])
            self.assertEqual(actual["rflags"], guest["rflags"])
            self.assertEqual(actual["rip"], 0x102000 + oracle.OFFSETS[completed])
            self.assertEqual(sum(a != before[p] for p, a in after.items()), 8 if vector == 12 else 12)

    def test_split_denial_preserves_bytes_rsp_and_defers_dirty(self):
        initial, before, pages, boundary = fixture()
        for scenario, second in (("split_np", 0), ("split_ro", 0x101005)):
            final, after, frames = oracle.prefix(initial, before, pages, 0, 0x30000, 0xA0080)
            self.assertEqual((final, after, frames), (initial, before, []))
            first_after, second_after = oracle.split_leaf_effects(0x100007, second, False)
            self.assertEqual(first_after, 0x100027)
            self.assertEqual(second_after, second)
            completed, vector, error, address = oracle.outcome(scenario, boundary)
            self.assertEqual((completed, vector, address), (0, 14, boundary))
            self.assertEqual(error, 2 if scenario == "split_np" else 3)
        self.assertEqual(oracle.split_leaf_effects(0x100007, 0x101007, True), (0x100067, 0x101067))
        self.assertEqual(oracle.split_leaf_effects(0x100067, 0, False), (0x100067, 0))

    def test_span_and_route_controls(self):
        _, _, pages, boundary = fixture()
        self.assertEqual(oracle.fragments(boundary - 4, 8), [(boundary - 4, 4), (boundary, 4)])
        for address in (oracle.HIGH - 4, (1 << 47) - 4, (1 << 64) - 4):
            with self.assertRaises(ValueError):
                oracle.fragments(address, 8)
        with self.assertRaises(ValueError):
            oracle.physical_bytes(boundary - 4, 8, {boundary - 4096: 0x100000})
        with self.assertRaises(ValueError):
            oracle.split_leaf_effects(0x100007, 0x101005, True)

    def test_peer_retained_fault_has_no_second_push_writeback(self):
        initial, before, pages, _ = fixture()
        first, after, _ = oracle.prefix(initial, before, pages, 1, 0x30000, 0xA0080)
        fault = oracle.native_fault_frame(first, 1, 0x30000, 0xA0080)
        self.assertEqual(fault["x"][8], first["x"][8])
        self.assertEqual(fault["x"][20], 0x5153)
        self.assertNotEqual(fault["x"][20], first["x"][20])
        completed, memory = oracle.instruction(first, after, pages, 1, 0x30000, 0xA0080)
        self.assertEqual(completed["x"][8], fault["x"][8] - 2)
        self.assertEqual(sum(memory[p] != after[p] for p in after), 2)

    def test_strict_entry_rejects_incomplete_evidence(self):
        with self.assertRaises(ValueError):
            oracle.check({})

    def test_before_wave_prefix_is_not_a_stopped_vm(self):
        record = baseline_fixture(n=2)
        oracle.baseline.check(record)
        with self.assertRaises(ValueError):
            oracle.baseline.check(record, before_wave=True)
        record["finished"] = 0
        oracle.baseline.check(record, before_wave=True)
        with self.assertRaises(ValueError):
            oracle.baseline.check(record)
        record["stack_memory_after"][32] ^= 1
        with self.assertRaises(ValueError):
            oracle.baseline.check(record, before_wave=True)

    def test_full_prefix_binding_rejects_fabricated_boundary_seed(self):
        wrapper = wrapper_fixture()
        record, shared, owners = wrapper["prefix"], wrapper["boundary"], wrapper["owners"]
        oracle.check_prefix(record, shared, owners, 2, 2)
        corrupted = copy.deepcopy(owners)
        corrupted[0]["before_frame"]["x"][8] += 12
        with self.assertRaises(ValueError):
            oracle.check_prefix(record, shared, corrupted, 2, 2)
        shared["data_before"][32] ^= 1
        with self.assertRaises(ValueError):
            oracle.check_prefix(record, shared, owners, 2, 2)

    def test_request_identity_and_captured_code(self):
        record = wrapper_fixture(n=3, profile=4, actor=2)
        oracle.check_envelope(record, 4, 3, 2, "low")
        for field, value in (("configured_vcpus", True), ("profile", 3), ("actor", 1),
                             ("outcome", "split"), ("region", "high"), ("tested_vcpus", 2)):
            corrupted = copy.deepcopy(record)
            corrupted[field] = value
            with self.assertRaises(ValueError):
                oracle.check_envelope(corrupted, 4, 3, 2, "low")
        record["boundary"]["code_before"][12] ^= 1
        with self.assertRaises(ValueError):
            oracle.check_envelope(record, 4, 3, 2, "low")

    def test_two_fragment_table_model_defers_dirty_and_preserves_ancestors(self):
        for second, error in ((0, 2), (0x100005, 3), (0x100007, 0)):
            tables = oracle.prefix_tables(3)
            oracle.table_word(tables, 0x4000 + 260 * 8, 0x101007)
            oracle.table_word(tables, 0x4000 + 261 * 8, second)
            after, routes, fault = oracle.split_readiness(tables, oracle.BOUNDARY - 4)
            self.assertEqual(fault, error)
            self.assertEqual(routes[0][2]["gpa"], 0x101FFC)
            self.assertEqual(routes[0][2]["after"], 0x101027)
            self.assertEqual(oracle.table_word(after, 0x4000 + 260 * 8), 0x101027 if error else 0x101067)
            self.assertEqual(oracle.table_word(after, 0x4000 + 261 * 8), second if error else 0x100067)
            touched = set(range(0x3000 + 260 * 8, 0x3000 + 262 * 8))
            self.assertTrue(all(after[i] == tables[i] for i in range(16384) if i not in touched))

    def test_all_fifty_bounded_ownership_schedules(self):
        for n in range(1, 5):
            for actor in range(n):
                for profile in range(1, 6):
                    with self.subTest(n=n, actor=actor, profile=profile):
                        oracle.check_schedule(schedule_fixture(n, profile, actor), n, profile, actor)

    def test_early_guest_fault_peer_resume_mapping_and_retirement_rejected(self):
        original = schedule_fixture()
        for moved, before in ((43, 2), (47, 42), (46, 47), (48, 5), (49, 48), (50, 49), (51, 50)):
            corrupted = copy.deepcopy(original)
            trace = corrupted["wave"]["trace"]
            word = next(word for word in trace if word >> 8 == moved)
            trace.remove(word)
            trace.insert(next(i for i, word in enumerate(trace) if word >> 8 == before), word)
            with self.subTest(moved=moved, before=before), self.assertRaises(ValueError):
                oracle.check_schedule(corrupted, 3, 1, 1)

    def test_split_service_cannot_run_before_detached_peers(self):
        for profile, event in ((3, 44), (4, 45), (5, 45)):
            corrupted = schedule_fixture(4, profile, 2)
            trace = corrupted["wave"]["trace"]
            word = (event << 8) | 2
            trace.remove(word)
            trace.insert(next(i for i, word in enumerate(trace) if word >> 8 == 2), word)
            with self.assertRaises(ValueError):
                oracle.check_schedule(corrupted, 4, profile, 2)

    def test_full_reports_all_outcomes_and_owner_counts(self):
        for n in range(1, 5):
            for profile in range(1, 6):
                with self.subTest(n=n, profile=profile):
                    result = oracle.audit_development(full_fixture(n, profile, n - 1))
                    self.assertEqual(result["native_faults"], n - 1 + int(profile >= 3))

    def test_corrupted_precise_state_route_memory_dirty_and_code_rejected(self):
        paths = [(('owners', 0, 'current', 'x', 8), 1),
                 (('owners', 0, 'final_guest', 'control', 1), 1),
                 (('owners', 0, 'data_after_service', 0), 1),
                 (('owners', 0, 'dirty_after', 0), 64),
                 (('owners', 0, 'fragments', 0, 'gpa'), 4096),
                 (('owners', 1, 'fault_frame', 'x', 8), 2),
                 (('owners', 1, 'saved_page'), 4096),
                 (('owners', 1, 'final_lower_leaf', 256), 128),
                 (('owners', 0, 'final_code', 0), 1),
                 (('boundary', 'code_at_outcome', 0), 1),
                 (('data_after', 42), 1), (('tables_after', 42), 1)]
        original = full_fixture(3, 4, 0)
        for path, mask in paths:
            corrupted = copy.deepcopy(original)
            target = corrupted
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] ^= mask
            with self.subTest(path=path), self.assertRaises(ValueError):
                oracle.audit_development(corrupted)

    def test_actual_store_fault_requires_valid_far_and_direct_access(self):
        original = full_fixture(3, 4, 0)
        for owner in (0, 1):
            for bit in (7, 8, 10):
                corrupted = copy.deepcopy(original)
                corrupted["owners"][owner]["esr"] ^= 1 << bit
                with self.subTest(owner=owner, bit=bit), self.assertRaises(ValueError):
                    oracle.audit_development(corrupted)

    def test_strict_acceptance_identity_schema_and_full_replay(self):
        original = full_fixture(3, 4, 0)
        result = oracle.check(original, 4, 3, 0, "low")
        self.assertEqual(result["completed_instructions"], 10)
        for arguments in ((3, 3, 0, "low"), (4, 4, 0, "low"), (4, 3, 1, "low"), (4, 3, 0, "high")):
            with self.assertRaises(ValueError):
                oracle.check(original, *arguments)
        for mutation in ("missing", "extra", "bool", "partial", "effect"):
            corrupted = copy.deepcopy(original)
            if mutation == "missing":
                del corrupted["source_after"]
            elif mutation == "extra":
                corrupted["owners"][0]["unverified_execution"] = 1
            elif mutation == "bool":
                corrupted["profile"] = True
            elif mutation == "partial":
                corrupted["boundary"]["wave"]["trace"].pop()
            else:
                corrupted["owners"][0]["final_guest"]["gpr"][4] -= 8
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                oracle.check(corrupted)


if __name__ == "__main__":
    unittest.main()

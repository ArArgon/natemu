#!/usr/bin/env python3
"""Independent sustained-maintenance oracle: FIFO identities, fairness and effects.

Replays observed ownership events as a queue and per-owner pending requests,
without importing the Rust scheduler/reservation policy. Ordinary intervals must
execute real OR semantics against declared physical-page inputs. The older
compose/event prefix remains checked by its existing independent oracle.
"""

import copy
import math

import check_i4 as base
from check_i1 import equal, require, state, uint, values

NO_CPU = (1 << 64) - 1


def check(record, expected_vcpus=None, expected_delay=None, expected_profile=None):
    equal(record["profile"], "i4-pressure", "pressure profile")
    if expected_profile is not None:
        equal(record["profile"], expected_profile, "requested pressure profile")
    trace = record["trace"]
    require(type(trace) is list and 0 < len(trace) <= 512, "bounded pressure trace")
    for word in trace:
        uint(word, 64, "trace word")
    start = next((i for i, word in enumerate(trace) if word >> 8 >= 23), None)
    require(start is not None, "missing sustained maintenance execution")
    prefix = dict(record, profile="i4-events", trace=trace[:start])
    base.check(prefix, expected_vcpus, expected_delay, "i4-events")
    n = record["configured_vcpus"]
    m = record["maintenance"]
    require(type(m) is dict, "maintenance summary required")
    intervals = m["intervals"]
    require(type(intervals) is list and 0 < len(intervals) <= 4 * n,
            "bounded actual ordinary intervals required")
    ordered = {}
    for cell in intervals:
        ordinal = uint(cell["ordinal"], 64, "interval ordinal")
        require(ordinal not in ordered and 1 <= ordinal <= len(intervals),
                "duplicate or missing ordinary interval identity")
        ordered[ordinal] = cell

    submitted, completed = [0] * n, [0] * n
    pending = {}
    queue = []
    current = None
    acknowledgements = set()
    serial = total = batch = duplicates = depth = 0
    ordinary = None
    ordinary_count = 0
    counts = [0] * n
    closing = dirty = False
    history = []
    leaves = []
    previous = None
    # Disjoint per-owner spans in two physically distinct pages.
    memory = {}
    frames = {}
    for cpu in range(n):
        old = next(c for c in record["cells"] if c["id"] == cpu)
        new = next(c for c in record["new_cells"] if c["id"] == cpu)
        memory[(0, cpu)] = list(old["memory_after"])
        memory[(1, cpu)] = list(new["memory_after"])
        frames[cpu] = copy.deepcopy(new["captured"])
    for word in trace[start:]:
        event, cpu = word >> 8, word & 255
        require(cpu < n, "maintenance participant outside configuration")
        if event == 23:
            require(cpu not in pending and cpu != ordinary and submitted[cpu] < 9,
                    "second pending operation, exhausted owner or active-owner submission")
            serial += 1
            submitted[cpu] += 1
            pending[cpu] = serial
            queue.append((cpu, serial))
            depth = max(depth, len(pending))
            require(len(pending) <= n, "operation storage capacity exceeded")
        elif event == 24:
            require(previous == (23, cpu) and cpu in pending,
                    "duplicate rejection must preserve the just-submitted original request")
            duplicates += 1
        elif event == 25:
            require(current is None and ordinary is None and queue and batch < 8,
                    "mutation overlaps execution/service or exceeds eight-head budget")
            current = queue.pop(0)
            equal(cpu, current[0], "FIFO protected operation owner")
            equal(pending[cpu], current[1], "original pending request identity")
            acknowledgements = set()
        elif event == 26:
            require(current is not None and ordinary is None and cpu not in acknowledgements,
                    "mapping acknowledgement without mutation or duplicated")
            last = (3 + total) % n
            if cpu == last:
                equal(acknowledgements, set(range(n)) - {last}, "delayed maintenance acknowledgement")
            acknowledgements.add(cpu)
        elif event == 27:
            require(current is not None and current[0] == cpu and ordinary is None,
                    "completion does not own the protected operation")
            equal(acknowledgements, set(range(n)), "each head completes only after all TLB acknowledgements")
            equal(pending.pop(cpu), current[1], "completion consumes original request")
            history.append(list(current))
            current = None
            completed[cpu] += 1
            total += 1
            batch += 1
            leaves.append((base.DATA + (4096 if total % 2 == 0 else 0)) | 0x27)
        elif event == 29:
            require(ordinary is None and current is None and cpu not in pending and 1 <= batch <= 8,
                    "ordinary work admitted over pending guest operation or invalid batch")
            equal(cpu, ordinary_count % n, "round-robin ordinary owner")
            require(counts[cpu] < 4, "per-owner interval capacity exhausted")
            ordinary = cpu
            closing = dirty = False
            ordinal = ordinary_count + 1
            require(ordinal in ordered, "actual ordinary interval missing")
            cell = ordered[ordinal]
            equal(uint(cell["id"], 32, "ordinary owner"), cpu, "interval owner")
            equal(uint(cell["heads"], 64, "interval heads"), total, "interval follows actual serviced heads")
            equal(uint(cell["epoch"], 64, "interval epoch"), 2 + total, "current maintenance generation")
        elif event == 28:
            require(ordinary is not None and not closing and (n == 1 or cpu != ordinary),
                    "closure must arrive once from a peer while ordinary grant remains active")
            closing = True
        elif event == 31:
            require(ordinary == cpu and closing and not dirty and current is None,
                    "dirty repair outside retained ordinary grant or after conflicting mutation")
            dirty = True
        elif event == 30:
            require(ordinary == cpu and closing and dirty and current is None,
                    "ordinary interval skipped native fault/completion")
            cell = ordered[ordinary_count + 1]
            page = 1 if total % 2 == 0 else 0
            expected_memory = list(memory[(page, cpu)])
            expected_memory[:8] = (0x4A00000000000000 | total << 16 | cpu).to_bytes(8, "little")
            expected_memory[128:144] = [0xA5] * 16
            check_interval(cell, frames[cpu], expected_memory, page, cpu)
            memory[(page, cpu)] = list(cell["memory_after"])
            frames[cpu] = copy.deepcopy(cell["captured"])
            counts[cpu] += 1
            ordinary_count += 1
            ordinary = None
            batch = 0
        else:
            raise ValueError(f"unknown maintenance event {event}")
        previous = (event, cpu)
    require(not pending and not queue and current is None and ordinary is None,
            "unfinished maintenance ownership at termination")
    equal(submitted, [9] * n, "nine actual submissions per participant")
    equal(completed, [9] * n, "nine completed mutations per participant")
    require(ordinary_count >= math.ceil(total / 8) and all(counts),
            "sustained pressure did not give every participant ordinary progress")
    equal(ordinary_count, len(intervals), "exact actual ordinary execution count")
    scalars = {"ready": (1 << n) - 1, "serial": serial, "length": 0,
               "epoch": 2 + total, "total": total, "batch": 0,
               "next_ordinary": ordinary_count % n, "ordinary": NO_CPU,
               "ordinary_total": ordinary_count, "finished": 1,
               "duplicate_rejections": duplicates, "maximum_depth": n,
               "leaf_final": (base.DATA + (4096 if total % 2 == 0 else 0)) | 0x67}
    for field, expected in scalars.items():
        equal(uint(m[field], 64, field), expected, "maintenance " + field)
    equal(depth, n, "all configured operation slots actually saturated")
    equal(duplicates, serial, "every duplicate attempt rejected without extra submission")
    for field, expected in (("submitted", submitted), ("completed", completed),
                            ("pending", [0] * n), ("interval_counts", counts)):
        equal(values(m[field], 4, 64, field), expected + [0] * (4 - n), field)
    equal(values(m["current"], 2, 64, "current"), [NO_CPU, 0], "no current operation")
    equal(values(m["leaf_history"], total, 64, "leaf_history"), leaves,
          "real guest PTE mutation and accessed effects before each completion")
    require(type(m["request_history"]) is list and len(m["request_history"]) == total,
            "one original identity per completed protected operation")
    for actual, expected in zip(m["request_history"], history):
        equal(values(actual, 2, 64, "request_history entry"), expected, "FIFO request history")


def check_interval(cell, previous, before, page, cpu):
    equal(cell["case"], "live_or", "ordinary generated fixture")
    equal(uint(cell["gpa"], 64, "ordinary GPA"), base.DATA + page * 4096 + cpu * 256,
          "ordinary access uses current physical route")
    equal(uint(cell["memory_base"], 64, "ordinary VA"), base.DATA + cpu * 256,
          "ordinary guest address remains stable")
    equal(uint(cell["object_pc"], 64, "ordinary PC"), base.CODE_NEXT, "current-source code slot")
    equal(uint(cell["capture_pc"], 64, "ordinary return"), previous["x"][30], "return target continuity")
    initial = state(cell["initial"], "ordinary initial")
    for field in previous:
        equal(initial[field], previous[field], "ordinary state continuity " + field)
    equal(values(cell["memory_before"], 256, 8, "ordinary inputs"), before,
          "declared fixture input and all unaffected physical bytes")
    bank = base.seed(cpu)[1]
    equal(values(cell["bank_before"], 512, 8, "ordinary bank"), bank, "private bank seed")
    base.check_instruction(cell)
    equal(uint(cell["dirty_faults"], 64, "ordinary fault count"), 1, "one real ordinary store fault")
    fault = cell["dirty_fault"]
    expected = base.store_fault_frame(initial, before, True)
    actual = state(fault["frame"], "ordinary dirty frame")
    for field in expected:
        equal(actual[field], expected[field], "ordinary dirty state " + field)
    equal(uint(fault["far"], 64, "ordinary FAR"), base.DATA + cpu * 256 + 128, "ordinary store address")
    equal(uint(fault["pc"], 64, "ordinary fault PC"), base.CODE_NEXT + 36, "ordinary store site")
    esr = uint(fault["esr"], 64, "ordinary ESR")
    equal(esr >> 26, 0x25, "ordinary same-EL abort")
    equal(esr & (1 << 6), 1 << 6, "ordinary native write")
    require(esr & 63 in range(12, 16), "ordinary permission fault required")
    equal(esr & ((1 << 10) | (1 << 8) | (1 << 7)), 0, "ordinary direct fault provenance")
    spsr = uint(fault["spsr"], 64, "ordinary SPSR")
    equal(spsr & 0xF0000000, expected["nzcv"], "ordinary pre-CFINV flags")
    equal(spsr & 0x3DF, 0x3C4, "ordinary masked EL1t")


def known_result(n):
    """A valid alternative schedule: full FIFO rounds and at most eight heads.

    This intentionally does not copy the runtime's asynchronous reservation policy.
    """
    r = base.known_events_result(n, 50)
    r["profile"] = "i4-pressure"
    memory = {(page, cpu): list(r["cells" if page == 0 else "new_cells"][cpu]["memory_after"])
              for page in range(2) for cpu in range(n)}
    frames = [copy.deepcopy(c["captured"]) for c in r["new_cells"]]
    trace = r["trace"]
    serial = total = batch = ordinary = 0
    counts = [0] * n
    history, intervals, leaves = [], [], []
    for iteration in range(9):
        requests = []
        for cpu in range(n):
            serial += 1
            requests.append([cpu, serial])
            trace += [(23 << 8) | cpu, (24 << 8) | cpu]
        for cpu, identity in requests:
            trace.append((25 << 8) | cpu)
            last = (3 + total) % n
            trace += [(26 << 8) | participant for participant in list(set(range(n)) - {last}) + [last]]
            trace.append((27 << 8) | cpu)
            total += 1
            batch += 1
            history.append([cpu, identity])
            leaves.append((base.DATA + (4096 if total % 2 == 0 else 0)) | 0x27)
        if batch + n > 8 or iteration == 8:
            cpu = ordinary % n
            trace += [(29 << 8) | cpu, (28 << 8) | ((cpu + 1) % n),
                      (31 << 8) | cpu, (30 << 8) | cpu]
            page = 1 if total % 2 == 0 else 0
            before = list(memory[(page, cpu)])
            before[:8] = (0x4A00000000000000 | total << 16 | cpu).to_bytes(8, "little")
            before[128:144] = [0xA5] * 16
            initial = copy.deepcopy(frames[cpu])
            dirty = base.store_fault_frame(initial, before, True)
            captured = copy.deepcopy(dirty)
            captured["nzcv"] ^= 1 << 29
            after = list(before)
            after[128:144] = captured["q"][16]
            bank = base.seed(cpu)[1]
            intervals.append({"id": cpu, "ordinal": ordinary + 1, "heads": total, "epoch": total + 2,
                              "gpa": base.DATA + page * 4096 + cpu * 256, "case": "live_or", "status": 1,
                              "initial": initial, "captured": captured, "memory_base": base.DATA + cpu * 256,
                              "memory_before": before, "memory_after": after, "bank_before": bank,
                              "bank_after": list(bank), "object_pc": base.CODE_NEXT,
                              "capture_pc": initial["x"][30], "dirty_faults": 1,
                              "dirty_fault": {"frame": dirty, "esr": (0x25 << 26) | 64 | 15,
                                              "far": base.DATA + cpu * 256 + 128, "pc": base.CODE_NEXT + 36,
                                              "spsr": dirty["nzcv"] | 0x3C4}})
            memory[(page, cpu)], frames[cpu] = after, captured
            counts[cpu] += 1
            ordinary += 1
            batch = 0
    r["maintenance"] = {"ready": (1 << n) - 1, "serial": serial, "submitted": [9] * n + [0] * (4 - n),
                        "completed": [9] * n + [0] * (4 - n), "pending": [0] * 4, "length": 0,
                        "current": [NO_CPU, 0], "epoch": total + 2, "total": total, "batch": 0,
                        "next_ordinary": ordinary % n, "ordinary": NO_CPU, "ordinary_total": ordinary,
                        "interval_counts": counts + [0] * (4 - n), "finished": 1,
                        "duplicate_rejections": serial, "maximum_depth": n, "leaf_history": leaves,
                        "leaf_final": leaves[-1] | 64, "request_history": history, "intervals": intervals}
    return r


def self_test():
    for n in range(1, 5):
        check(known_result(n), n, 50, "i4-pressure")
    original = known_result(4)
    start = next(i for i, word in enumerate(original["trace"]) if word >> 8 == 23)
    ordinary_at = next(i for i, word in enumerate(original["trace"]) if word >> 8 == 29)
    mutations = [
        ("wrong original request", lambda r: r["maintenance"]["request_history"][0].__setitem__(1, 99)),
        ("duplicate request serial", lambda r: r["maintenance"]["request_history"][1].__setitem__(1, 1)),
        ("early head completion", lambda r: r["trace"].insert(start + 9, 27 << 8)),
        ("second pending submission", lambda r: r["trace"].insert(start + 1, 23 << 8)),
        ("wrong FIFO owner", lambda r: r["trace"].__setitem__(start + 8, (25 << 8) | 1)),
        ("missing duplicate rejection", lambda r: r["trace"].pop(start + 1)),
        ("lost pending operation", lambda r: r["maintenance"]["pending"].__setitem__(2, 1)),
        ("missing real mutation", lambda r: r["maintenance"]["leaf_history"].__setitem__(0, 0x101027)),
        ("missing final dirty bit", lambda r: r["maintenance"].__setitem__("leaf_final", 0x101027)),
        ("wrong current route", lambda r: r["maintenance"]["intervals"][0].__setitem__("gpa", base.DATA)),
        ("ordinary body skipped", lambda r: r["maintenance"]["intervals"][0].__setitem__("captured", r["maintenance"]["intervals"][0]["initial"])),
        ("sentinel store skipped", lambda r: r["maintenance"]["intervals"][0].__setitem__("memory_after", r["maintenance"]["intervals"][0]["memory_before"])),
        ("lost state continuity", lambda r: r["maintenance"]["intervals"][0]["initial"]["x"].__setitem__(19, 0)),
        ("stale epoch", lambda r: r["maintenance"]["intervals"][0].__setitem__("epoch", 2)),
        ("unobserved interval", lambda r: r["maintenance"]["intervals"].pop()),
        ("dirty fault lost load", lambda r: r["maintenance"]["intervals"][0]["dirty_fault"]["frame"]["x"].__setitem__(4, 0)),
        ("missing active closure", lambda r: r["trace"].remove(next(w for w in r["trace"] if w >> 8 == 28))),
        ("missing native fault", lambda r: r["trace"].remove(next(w for w in r["trace"] if w >> 8 == 31))),
        ("eight-head fairness skipped", lambda r: r["trace"].__delitem__(slice(ordinary_at, ordinary_at + 4))),
        ("round-robin owner skipped", lambda r: r["trace"].__setitem__(ordinary_at, (29 << 8) | 1)),
        ("delayed acknowledgement early", lambda r: r["trace"].__setitem__(start + 9, (26 << 8) | 3)),
    ]
    for name, mutate in mutations:
        bad = copy.deepcopy(original)
        mutate(bad)
        try:
            check(bad, 4, 50, "i4-pressure")
        except (ValueError, KeyError, TypeError):
            continue
        raise ValueError("accepted pressure corruption: " + name)
    print(f"PASS: N=1/2/3/4 pressure references; {len(mutations)} pressure corruption controls")


if __name__ == "__main__":
    self_test()

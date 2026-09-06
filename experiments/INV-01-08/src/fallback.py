"""F08-2 pure raw oracle. No authorization or observations are manufactured here."""
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction

U64 = (1 << 64) - 1
U32 = (1 << 32) - 1


def integer(value, minimum=0, maximum=U64):
    return type(value) is int and minimum <= value <= maximum


def validate_fallback(data):
    """Return F1–F4 failures; only an empty list permits later timing gates."""
    failures = []

    def rule(ok, group, detail):
        if not ok:
            failures.append(f"FAIL {group}: {detail}")

    f = data.get("fallback_global_lock") if isinstance(data, dict) else None
    if not isinstance(f, dict):
        return ["FAIL F1: missing fallback object", "FAIL F4: incomplete run"]
    constants = {
        "threads": 4, "allocation_size": 256, "allocation_alignment": 128,
        "target_offset": 78, "target_width": 4, "initial_value": 0,
        "expected_value": 1000000, "expected_ops": 1000000,
    }
    rule(f.get("schema") == "INV-01-08-fallback-v2", "F1", "schema")
    for key, expected in constants.items():
        rule(integer(f.get(key)) and f[key] == expected, "F1", key)
    numeric = ("allocation_address", "final_value", "ops", "completed_ops",
               "actual_writes", "lost_updates", "canary_before_initial",
               "canary_before_final", "canary_after_initial", "canary_after_final",
               "created", "joined", "init_rc", "destroy_rc", "start_ticks",
               "end_ticks", "elapsed_ns", "timebase_numer", "timebase_denom",
               "timebase_rc")
    for key in numeric:
        rule(integer(f.get(key)), "F1", key + " unsigned integer")
    address = f.get("allocation_address")
    rule(integer(address, 128, U64 - 255) and address % 128 == 0,
         "F1", "allocation address alignment/bounds")
    for key in ("final_value", "canary_before_initial", "canary_before_final",
                "canary_after_initial", "canary_after_final"):
        rule(integer(f.get(key), 0, U32), "F1", key + " 32-bit bound")
    timing_keys = ("start_ticks", "end_ticks", "elapsed_ns", "timebase_numer", "timebase_denom")
    timing = all(integer(f.get(k), 1) for k in timing_keys)
    timing = timing and f["end_ticks"] > f["start_ticks"]
    if timing:
        derived = ((f["end_ticks"] - f["start_ticks"]) * f["timebase_numer"] // f["timebase_denom"])
        timing = 0 < derived <= U64 and derived == f["elapsed_ns"]
    rule(timing and f.get("timing_valid") is True, "F1", "exact positive tick conversion")
    rule(integer(f.get("timebase_numer"), 1, U32) and
         integer(f.get("timebase_denom"), 1, U32), "F1", "timebase bounds")
    tb = data.get("timebase")
    rule(isinstance(tb, dict) and integer(tb.get("numer"), 1, U32) and
         integer(tb.get("denom"), 1, U32) and
         tb["numer"] == f.get("timebase_numer") and tb["denom"] == f.get("timebase_denom"),
         "F1", "top-level timebase agreement")
    if "ns_per_op" in f:
        value = f["ns_per_op"]
        valid = type(value) in (int, float) and timing and integer(f.get("completed_ops"), 1)
        if valid:
            with localcontext() as ctx:
                ctx.prec = 100
                actual = Decimal(str(value))
                expected = (Decimal(f["elapsed_ns"]) / Decimal(f["completed_ops"])).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_EVEN)
                valid = actual.is_finite() and actual > 0 and actual == expected
        rule(valid, "F1", "optional rounded display agrees with exact ratio")
    workers = f.get("workers")
    valid_workers = isinstance(workers, list) and len(workers) == 4 and all(isinstance(w, dict) for w in workers)
    rule(valid_workers, "F1", "exact worker coverage")
    if valid_workers:
        ids = [w.get("id") for w in workers]
        rule(all(integer(i, 0, 3) for i in ids) and sorted(ids) == list(range(4)), "F1", "worker IDs")
        for w in workers:
            for key in ("requested_iterations", "completed_iterations", "actual_writes",
                        "successful_locks", "successful_unlocks"):
                rule(integer(w.get(key), 0, 250000), "F1", "worker " + key)
                rule(type(w.get(key)) is int and w[key] == 250000, "F2", "worker " + key + " complete")
            rule(w.get("ready") is True and w.get("completed") is True, "F3", "worker ready/completed")
            rule(w.get("qos") == "user-interactive" and type(w.get("qos_rc")) is int and w["qos_rc"] == 0,
                 "F3", "worker QoS success")
            rule("first_error" in w and w["first_error"] is None, "F3", "worker error absent")
        for total, member in (("completed_ops", "completed_iterations"), ("actual_writes", "actual_writes")):
            rule(all(integer(w.get(member)) for w in workers) and integer(f.get(total)) and
                 sum(w.get(member, 0) for w in workers if integer(w.get(member))) == f.get(total),
                 "F2", total + " worker sum")
    for key in ("final_value", "expected_value", "expected_ops", "ops", "completed_ops", "actual_writes"):
        rule(type(f.get(key)) is int and f[key] == 1000000, "F2", key + " observed agreement")
    rule(type(f.get("lost_updates")) is int and f["lost_updates"] == 0, "F2", "no lost updates")
    for key in ("init_rc", "destroy_rc", "timebase_rc"):
        rule(type(f.get(key)) is int and f[key] == 0, "F3", key + " success")
    for key in ("created", "joined"):
        rule(type(f.get(key)) is int and f[key] == 4, "F3", key + " complete")
    for key in ("create_rc", "join_rc"):
        a = f.get(key)
        rule(isinstance(a, list) and len(a) == 4 and all(type(v) is int and v == 0 for v in a),
             "F3", key + " exact success coverage")
    for side, expected in (("before", 0xa5a5a5a5), ("after", 0x5a5a5a5a)):
        rule(all(type(f.get("canary_" + side + suffix)) is int and
                 f["canary_" + side + suffix] == expected for suffix in ("_initial", "_final")),
             "F3", side + " canary unchanged")
    rule(data.get("experiment")=="INV-01-08" and data.get("schema")=="INV-01-08-result-v3" and
         data.get("method")=="F08-2" and isinstance(data.get("run_id"),str) and len(data["run_id"])==32,
         "F4", "producing method/run schema")
    rule(data.get("mode") == "normal" and data.get("completed_normal") is True and
         data.get("standalone") is False and f.get("completed_normal") is True and
         f.get("injected") is False, "F4", "complete uninjected normal run only")
    return failures


def fallback_ratio(data):
    f = data["fallback_global_lock"]
    return Fraction(f["elapsed_ns"], f["completed_ops"])


def validate_full_counts(data):
    """Fixed full-method sample counts supplement, never relax, I1–I14."""
    errors = []
    if type(data.get("litmus_iterations")) is not int or data["litmus_iterations"] != 1000000:
        errors.append("FAIL S1: fixed full litmus count")
    for row in data.get("litmus", []):
        if type(row.get("iterations")) is not int or row["iterations"] != 1000000 or type(row.get("sequential")) is not bool:
            errors.append("FAIL S1: litmus count/mode types")
    for row in data.get("lse", []):
        n = row.get("threads")
        if (type(n) is not int or n not in (2, 4, 8) or row.get("qos") != "user-interactive" or
                any(not integer(row.get(k), 1) for k in ("ops", "counter", "attempts", "elapsed_ns")) or
                row.get("ops") != n * 1000000 or row.get("counter") != row.get("ops") or
                row.get("attempts", 0) < row.get("ops", 0)):
            errors.append("FAIL S1: LSE full count/primitive denominator")
    offsets = {"ldaddal aligned 4B (control)":64, "ldaddal misaligned within 16B granule":65,
               "ldaddal crossing 16-byte boundary":78, "ldaddal crossing 64-byte cache line":126,
               "plain ldr crossing 64-byte cache line":126, "ldar crossing 64-byte cache line":126}
    for row in data.get("alignment", []):
        if (type(row.get("offset")) is not int or row["offset"] != offsets.get(row.get("case")) or
                type(row.get("faulted")) is not bool or not integer(row.get("signal")) or not integer(row.get("si_code"))):
            errors.append("FAIL S1: alignment offset/types")
    for row in data.get("roundtrip", []):
        timing = [row.get(k) for k in ("min_ns", "p50_ns", "p99_ns", "max_ns")]
        if (type(row.get("n")) is not int or row["n"] != 20000 or
                type(row.get("timeouts")) is not int or row["timeouts"] != 0 or
                not all(integer(v, 1) for v in timing) or timing != sorted(timing)):
            errors.append("FAIL S1: full handshake count/positive ordered timing")
    return errors

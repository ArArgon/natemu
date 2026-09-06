#!/usr/bin/env python3
"""Author-created synthetic F08-2 fixtures, not independent oracle approval.

No native probe/control is launched. Mocked watchdog/process cases exercise
supervision logic without running a timer or generating research evidence.
"""
import contextlib
import copy
import errno
import io
import json
import os
import time
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import evidence as auth
import check
from fallback import validate_fallback, fallback_ratio, validate_full_counts
import supervisor as sup


def valid():
    d = json.loads((ROOT / "tests/valid.json").read_text())
    d.update(schema="INV-01-08-result-v3", method="F08-2", mode="normal",
             run_id="1" * 32, completed_normal=True, standalone=False,
             timebase={"numer": 125, "denom": 3})
    d["fallback_global_lock"] = {
        "schema": "INV-01-08-fallback-v2", "threads": 4, "allocation_size": 256,
        "allocation_alignment": 128, "allocation_address": 4096, "target_offset": 78,
        "target_width": 4, "initial_value": 0, "final_value": 1000000,
        "expected_value": 1000000, "expected_ops": 1000000, "ops": 1000000,
        "completed_ops": 1000000, "actual_writes": 1000000, "lost_updates": 0,
        "canary_before_initial": 0xa5a5a5a5, "canary_before_final": 0xa5a5a5a5,
        "canary_after_initial": 0x5a5a5a5a, "canary_after_final": 0x5a5a5a5a,
        "created": 4, "joined": 4, "init_rc": 0, "destroy_rc": 0,
        "create_rc": [0] * 4, "join_rc": [0] * 4,
        "start_ticks": 100, "end_ticks": 12000100, "elapsed_ns": 500000000,
        "timebase_numer": 125, "timebase_denom": 3, "timebase_rc": 0,
        "timing_valid": True, "injected": False, "completed_normal": True,
        "workers": [{"id": i, "qos": "user-interactive", "qos_rc": 0,
                     "ready": True, "completed": True, "requested_iterations": 250000,
                     "completed_iterations": 250000, "actual_writes": 250000,
                     "successful_locks": 250000, "successful_unlocks": 250000,
                     "first_error": None} for i in range(4)],
    }
    return d


def sequentialize(row):
    """Force an intended-negative sequential row while keeping its histogram
    a valid, self-consistent accounting (REV-210 R1): the relaxed-outcome
    cell must actually become zero, not merely the summary field."""
    row["sequential"] = True
    key = "r0=0,r1=0" if row["test"] == "SB" else "r0!=0,r1=0"
    moved = row["histogram"][key]
    row["histogram"][key] = 0
    row["histogram"]["r0=0,r1!=0"] += moved
    row["relaxed_observed"] = 0


def build_synthetic_campaign(parent):
    """Author-created parser fixture only; no native execution. Builds a
    complete five-member campaign directory and returns its packet, build,
    build digest and writer for integrity mutations."""
    context = {"host_id": "synthetic", "host_model": "synthetic", "os_build": "synthetic",
               "architecture": "arm64", "timebase": {"numer": 125, "denom": 3}}
    a={"schema":"INV-01-08-build-v2","outcome":"built","run_id":"1"*32,"source_sha256":"2"*64}
    build_dir=parent/"producing-build"
    (build_dir/"build").mkdir(parents=True)
    (build_dir/"build/litmus").write_text("dummy build, never executed")
    a["artifacts"]={"build/litmus":auth.file_sha(build_dir/"build/litmus")}
    build_manifest=build_dir/"build-manifest.json"
    build_manifest.write_bytes(auth.canonical(a))
    auth_sha=auth.file_sha(build_manifest)
    packet={"schema":"INV-01-08-campaign-v3","run_id":a["run_id"],"members":{}}

    def write(path, value):
        path.write_bytes(auth.canonical(value))

    for mode in ("normal", "negative", "fallback-drop-write", "fallback-lock-error", "unexpected-fault"):
        location = parent / mode
        location.mkdir()
        for name in ("check.txt","stderr.txt","environment-before.json","environment-after.json"):
            (location/name).write_text("synthetic")
        (location/"stdout.txt").write_text("")
        d = valid()
        d.update(run_id=a["run_id"],compiled_bundle_sha256=a["source_sha256"])
        code = 0
        if mode == "negative":
            d.update(mode=mode, completed_normal=False)
            for row in d["litmus"]:
                sequentialize(row)
        elif mode == "fallback-drop-write":
            d.update(mode=mode, completed_normal=False, standalone=True)
            f = d["fallback_global_lock"]
            f.update(injected=True, completed_normal=False, actual_writes=999999, final_value=999999, lost_updates=1)
            f["workers"][0]["actual_writes"] = 249999
            code = 1
        elif mode == "fallback-lock-error":
            d.update(mode=mode, completed_normal=False, standalone=True)
            f = d["fallback_global_lock"]
            f.update(injected=True, completed_normal=False, ops=0, completed_ops=0, actual_writes=0,
                     final_value=0, lost_updates=1000000)
            for w in f["workers"]:
                w.update(completed=False, completed_iterations=0, actual_writes=0, successful_locks=0,
                          successful_unlocks=0)
            f["workers"][0]["first_error"] = {"operation": "lock", "iteration": 0, "rc": errno.EIO, "injected": True}
            code = 1
        if mode != "unexpected-fault":
            write(location / "result.json", d)
        else:
            code = 90
        write(location/"invocation.json",dict(schema="INV-01-08-invocation-v2",run_id=a["run_id"],
               command=[str(build_dir/"build/litmus"),"--mode",mode,"--out",str(location/"result.json")],
               binary_sha256=a["artifacts"]["build/litmus"],build={"path":str(build_manifest),"sha256":auth_sha}))
        write(location/"result.supervisor.json",dict(schema="INV-01-08-supervisor-v2",run_id=a["run_id"],
               mode=mode,returncode=code,timed_out=False,context=context,
               result_sha256=auth.file_sha(location/"result.json") if mode!="unexpected-fault" else None))
        manifest = {"schema": "INV-01-08-execution-manifest-v2", "mode": mode,
                    "classification_returncode": 0, "run_id": a["run_id"],
                    "artifacts": {str(p.relative_to(location)): auth.file_sha(p)
                                  for p in location.rglob("*") if p.is_file()}}
        write(location / "manifest.json", manifest)
        packet["members"][mode] = {"path": str(location / "manifest.json"),
                                    "sha256": auth.file_sha(location / "manifest.json")}
    write(parent / "campaign.json", packet)
    write(parent / "schedule.json", {"run_id":a["run_id"]})
    write(parent / "campaign.supervisor.json", dict(schema="INV-01-08-campaign-supervisor-v3",
          run_id=a["run_id"],status="completed",returncode=0,timed_out=False,output_limited=False,
          watchdog_seconds=2700,output_limit_bytes=128*1024*1024,supervisor_elapsed_ns=1000,output_bytes=1000,
          modes={mode:"completed" for mode in packet["members"]},
          artifacts={str(p.relative_to(parent)):auth.file_sha(p) for p in parent.rglob("*") if p.is_file()}))
    return packet, a, auth_sha, write


class Fallback(unittest.TestCase):
    def rejected(self, d, group=None):
        errors = validate_fallback(d)
        self.assertTrue(errors)
        if group:
            self.assertTrue(any(group in line for line in errors), errors)

    def test_positive_and_strict_exact_boundary(self):
        d = valid()
        self.assertEqual(validate_fallback(d), [])
        self.assertEqual(fallback_ratio(d), 500)
        self.assertFalse(fallback_ratio(d) < 500)
        for delta in (-1, 1):
            r = copy.deepcopy(d)
            f = r["fallback_global_lock"]
            f["end_ticks"] += delta
            f["elapsed_ns"] = (f["end_ticks"] - f["start_ticks"]) * 125 // 3
            self.assertEqual(validate_fallback(r), [])
            self.assertEqual(fallback_ratio(r) < 500, delta < 0)

    def test_full_cli_exact_t2_screen_with_explicit_binding_mock(self):
        for delta, decision in ((0, "MISS"), (-1, "MEET"), (1, "MISS")):
            d = valid()
            f = d["fallback_global_lock"]
            f["end_ticks"] += delta
            f["elapsed_ns"] = (f["end_ticks"] - f["start_ticks"]) * 125 // 3
            output = io.StringIO()
            with patch.object(check, "json_read", return_value=d), \
                 patch.object(check, "validate_result_binding", return_value={"status":"context-matched"}), contextlib.redirect_stdout(output):
                self.assertEqual(check.cli(["synthetic-render-fixture"]), 0)
            self.assertIn(decision + "  T2", output.getvalue())

    def test_retained_i1_i4_direct_fixture_mutations(self):
        for mutation, expected in ((lambda d:d["litmus"].pop(), "FAIL  I1"),
                                   (lambda d:d["litmus"][0].update(iterations=999999), "FAIL  I4")):
            d = json.loads((ROOT/"tests/valid.json").read_text())
            mutation(d)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(check.main("synthetic", data=d, historical=True), 1)
            self.assertIn(expected, output.getvalue())
            self.assertNotIn("corroboration=", output.getvalue())

    def test_every_required_fallback_field_missing(self):
        for key in valid()["fallback_global_lock"]:
            with self.subTest(key=key):
                d = valid()
                d["fallback_global_lock"].pop(key)
                self.rejected(d)

    def test_every_required_worker_field_missing(self):
        for key in valid()["fallback_global_lock"]["workers"][0]:
            d = valid()
            d["fallback_global_lock"]["workers"][0].pop(key)
            self.rejected(d)

    def test_integer_types_bounds_and_boolean_confusion(self):
        original = valid()["fallback_global_lock"]
        for key, value in original.items():
            if type(value) is int:
                for bad in (True, False, -1, 1 << 64, "0", None, 0.0):
                    with self.subTest(key=key, value=bad):
                        d = valid()
                        d["fallback_global_lock"][key] = bad
                        self.rejected(d)
        for key, value in original["workers"][0].items():
            if type(value) is int:
                for bad in (True, -1, "0", None, 1 << 64):
                    d = valid()
                    d["fallback_global_lock"]["workers"][0][key] = bad
                    self.rejected(d)

    def test_api_count_canary_and_state_corruption(self):
        original = valid()["fallback_global_lock"]
        for key in original:
            if type(original[key]) is int:
                d = valid()
                d["fallback_global_lock"][key] += 1
                self.rejected(d)
        for key in ("create_rc", "join_rc"):
            for values in ([0, 0, 0], [0, 0, 0, 0, 0], [0, 0, 0, None], [0, 0, 0, errno.EIO], [False]*4):
                d = valid()
                d["fallback_global_lock"][key] = values
                self.rejected(d, "F3")
        for key in ("ready", "completed", "first_error", "qos", "qos_rc"):
            d = valid()
            d["fallback_global_lock"]["workers"][0][key] = "bad"
            self.rejected(d, "F3")
        for key in ("completed_normal", "injected", "timing_valid"):
            d = valid()
            d["fallback_global_lock"][key] = not d["fallback_global_lock"][key]
            self.rejected(d)

    def test_worker_coverage_and_all_top_level_fields(self):
        for bad in ([], [{}]*4, None, "workers", valid()["fallback_global_lock"]["workers"][:3]):
            d = valid()
            d["fallback_global_lock"]["workers"] = bad
            self.rejected(d, "F1")
        d = valid()
        d["fallback_global_lock"]["workers"][3]["id"] = 0
        self.rejected(d, "F1")
        for key in ("experiment", "schema", "method", "run_id", "mode", "completed_normal", "standalone", "timebase"):
            d = valid()
            d.pop(key)
            self.rejected(d)

    def test_optional_display_ties_even_and_nonfinite(self):
        for elapsed, display in ((5000000, 5.0), (5005000, 5.0), (5015000, 5.02)):
            d = valid()
            d["timebase"] = {"numer": 1, "denom": 1}
            f = d["fallback_global_lock"]
            f.update(timebase_numer=1, timebase_denom=1, end_ticks=100+elapsed,
                     elapsed_ns=elapsed, ns_per_op=display)
            self.assertEqual(validate_fallback(d), [])
        for bad in (float("nan"), float("inf"), -1, 0, True, "500", 499.99):
            d = valid()
            d["fallback_global_lock"]["ns_per_op"] = bad
            self.rejected(d, "F1")

    def test_zero_reverse_overflow_ticks(self):
        for changes in ({"start_ticks": 0}, {"end_ticks": 100}, {"end_ticks": 99},
                        {"end_ticks": (1 << 64)-1, "timebase_numer": (1 << 32)-1},
                        {"timebase_denom": 0}, {"elapsed_ns": 0}):
            d = valid()
            d["fallback_global_lock"].update(changes)
            self.rejected(d, "F1")

    def test_counterexample_and_new_run_failclosed_cli(self):
        d = valid()
        d["fallback_global_lock"] = {"threads": 0, "ops": 0, "elapsed_ns": 0, "ns_per_op": -1}
        self.rejected(d)
        for item in (d, valid()):
            output = io.StringIO()
            # No review mock: even a positive synthetic object has no actual
            # prerequisite receipt, so normal CLI cannot promote it.
            with patch.object(check, "json_read", return_value=item), contextlib.redirect_stdout(output):
                self.assertEqual(check.cli(["synthetic-not-evidence.json"]), 1)
            for forbidden in ("corroboration=", "  H ", "  MEET", "  MISS", "RESULT: PASS"):
                self.assertNotIn(forbidden, output.getvalue())

    def test_historical_explicit_no_upgrade(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(check.cli([str(ROOT / "tests/valid.json")]), 1)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(check.cli(["--historical", str(ROOT / "tests/valid.json")]), 0)
        text = output.getvalue()
        self.assertIn("T2/T3 UNQUALIFIED", text)
        self.assertNotIn("MEET  T2", text)
        self.assertNotIn("MISS  T2", text)
        self.assertNotIn("MEET  T3", text)
        self.assertNotIn("RESULT: PASS", text)
        with patch.object(check, "json_read", return_value=valid()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check.cli(["--historical", "synthetic"]), 1)

    def test_v2_historical_cohort_bypass_rejected(self):
        cohort = ("synthetic", "0"*64, "not-an-approval", [("synthetic", valid())])
        output = io.StringIO()
        with patch.object(check, "load_cohort", return_value=cohort), contextlib.redirect_stdout(output):
            self.assertEqual(check.cli(["--historical", "--cohort", "synthetic"]), 1)
        self.assertNotIn("corroboration=", output.getvalue())

    def test_v2_full_cli_late_render_failure_with_explicit_binding_mock(self):
        original = check.report
        def broken(data):
            original(data)
            raise RuntimeError("corroboration=ELIGIBLE MEET T2")
        output = io.StringIO()
        with patch.object(check, "json_read", return_value=valid()), \
             patch.object(check, "validate_result_binding", return_value={"status":"context-matched"}), \
             patch.object(check, "report", side_effect=broken), contextlib.redirect_stdout(output):
            self.assertEqual(check.cli(["synthetic-not-evidence"]), 1)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        for marker in ("corroboration=", "MEET", "MISS", " H "):
            self.assertNotIn(marker, output.getvalue())


class Controls(unittest.TestCase):
    def test_sequential_control_rejects_unrelated_failure(self):
        d = valid()
        d.update(mode="negative", completed_normal=False)
        for row in d["litmus"]:
            sequentialize(row)
        receipt = {"returncode":0, "timed_out":False}
        self.assertEqual(validate_full_counts(d), [])
        self.assertTrue(sup.sequential_oracle(d, receipt))
        d["lse"][0]["lost_updates"] = 1
        self.assertFalse(sup.sequential_oracle(d, receipt))
        d["lse"][0]["lost_updates"] = 0
        d["fallback_global_lock"]["final_value"] -= 1
        self.assertFalse(sup.sequential_oracle(d, receipt))

    def test_sequential_control_rejects_corrupted_histogram(self):
        # REV-210 R1 counterexample: every row's histogram cells forced to
        # (-1, 0, 0, 1000001) while the summary fields still show exactly the
        # intended I2/I3 shape. check.main used to return on I2/I3 before
        # validate_histograms ever ran, so this was wrongly accepted.
        d = valid()
        d.update(mode="negative", completed_normal=False)
        for row in d["litmus"]:
            sequentialize(row)
        for row in d["litmus"]:
            row["histogram"] = {"r0=0,r1=0": -1, "r0=0,r1!=0": 0, "r0!=0,r1=0": 0, "r0!=0,r1!=0": 1000001}
        receipt = {"returncode": 0, "timed_out": False}
        self.assertFalse(sup.sequential_oracle(d, receipt))

    def test_sequential_control_rejects_missing_and_mismatched_histogram(self):
        d = valid()
        d.update(mode="negative", completed_normal=False)
        for row in d["litmus"]:
            sequentialize(row)
        receipt = {"returncode": 0, "timed_out": False}
        self.assertTrue(sup.sequential_oracle(d, receipt))
        missing = copy.deepcopy(d)
        del missing["litmus"][0]["histogram"]
        self.assertFalse(sup.sequential_oracle(missing, receipt))
        mismatched = copy.deepcopy(d)
        mismatched["litmus"][0]["histogram"]["r0=0,r1!=0"] += 1  # sum now != iterations
        self.assertFalse(sup.sequential_oracle(mismatched, receipt))

    def test_full_counts_types_coverage_not_reduced(self):
        self.assertEqual(validate_full_counts(valid()), [])
        changes = (("litmus", "iterations", 999999), ("litmus", "sequential", 0),
                   ("lse", "ops", 1), ("lse", "counter", 1), ("lse", "attempts", 0),
                   ("alignment", "offset", 79), ("alignment", "faulted", 0),
                   ("roundtrip", "n", 19999), ("roundtrip", "timeouts", False),
                   ("roundtrip", "p99_ns", -1))
        for arm, key, value in changes:
            d = valid()
            d[arm][0][key] = value
            self.assertTrue(validate_full_counts(d), (arm, key, value))

    def test_drop_write_exact_oracle(self):
        d = valid()
        d.update(mode="fallback-drop-write", completed_normal=False, standalone=True)
        f = d["fallback_global_lock"]
        f.update(injected=True, completed_normal=False, actual_writes=999999, final_value=999999, lost_updates=1)
        f["workers"][0]["actual_writes"] = 249999
        receipt = {"returncode": 1, "timed_out": False}
        self.assertTrue(sup.control_oracle(d["mode"], receipt, d, ""))
        for key in ("final_value", "actual_writes", "completed_ops", "canary_after_final"):
            mutated = copy.deepcopy(d)
            mutated["fallback_global_lock"][key] -= 1
            self.assertFalse(sup.control_oracle(d["mode"], receipt, mutated, ""))
        self.assertFalse(sup.control_oracle(d["mode"], dict(receipt, timed_out=True), d, ""))

    def test_injected_lock_error_no_rmw_or_claim(self):
        d = valid()
        d.update(mode="fallback-lock-error", completed_normal=False, standalone=True)
        f = d["fallback_global_lock"]
        for w in f["workers"]:
            w.update(completed=False, completed_iterations=0, actual_writes=0, successful_locks=0, successful_unlocks=0)
        f["workers"][0]["first_error"] = {"operation": "lock", "iteration": 0, "rc": errno.EIO, "injected": True}
        f.update(injected=True, completed_normal=False, ops=0, completed_ops=0, actual_writes=0, final_value=0, lost_updates=1000000)
        receipt = {"returncode": 1, "timed_out": False}
        self.assertTrue(sup.control_oracle(d["mode"], receipt, d, ""))
        f["workers"][0]["actual_writes"] = 1
        self.assertFalse(sup.control_oracle(d["mode"], receipt, d, ""))

    def test_drop_write_rejects_concealed_original_status_and_bool_confusion(self):
        # REV-210 R2 counterexample: a drop-write raw with standalone/nested
        # completed_normal set to 'invalid', and lost_updates a bool (True
        # spuriously == 1), used to be accepted because the repaired copy
        # blindly restamps those exact fields before validate_fallback ever
        # inspects them.
        def base():
            d = valid()
            d.update(mode="fallback-drop-write", completed_normal=False, standalone=True)
            f = d["fallback_global_lock"]
            f.update(injected=True, completed_normal=False, actual_writes=999999, final_value=999999, lost_updates=1)
            f["workers"][0]["actual_writes"] = 249999
            return d
        receipt = {"returncode": 1, "timed_out": False}
        self.assertTrue(sup.control_oracle("fallback-drop-write", receipt, base(), ""))
        for mutate in (
                lambda x: x.update(standalone="invalid"),
                lambda x: x.__delitem__("standalone"),
                lambda x: x["fallback_global_lock"].update(completed_normal="invalid"),
                lambda x: x["fallback_global_lock"].__delitem__("completed_normal"),
                lambda x: x["fallback_global_lock"].update(lost_updates=True)):
            mutated = base()
            mutate(mutated)
            self.assertFalse(sup.control_oracle("fallback-drop-write", receipt, mutated, ""))

    def test_lock_error_rejects_concealed_original_status_and_bool_confusion(self):
        # REV-210 R2 counterexample: a lock-error raw with
        # ops/completed_ops/actual_writes/final_value and worker0's counts
        # all bools instead of ints (False spuriously == 0), plus
        # first_error.iteration as False, used to be accepted.
        def base():
            d = valid()
            d.update(mode="fallback-lock-error", completed_normal=False, standalone=True)
            f = d["fallback_global_lock"]
            for w in f["workers"]:
                w.update(completed=False, completed_iterations=0, actual_writes=0, successful_locks=0,
                          successful_unlocks=0)
            f["workers"][0]["first_error"] = {"operation": "lock", "iteration": 0, "rc": errno.EIO, "injected": True}
            f.update(injected=True, completed_normal=False, ops=0, completed_ops=0, actual_writes=0,
                     final_value=0, lost_updates=1000000)
            return d
        receipt = {"returncode": 1, "timed_out": False}
        self.assertTrue(sup.control_oracle("fallback-lock-error", receipt, base(), ""))
        for mutate in (
                lambda x: x.update(standalone="invalid"),
                lambda x: x.__delitem__("standalone"),
                lambda x: x["fallback_global_lock"].update(completed_normal="invalid"),
                lambda x: x["fallback_global_lock"].update(ops=False, completed_ops=False,
                                                            actual_writes=False, final_value=False),
                lambda x: x["fallback_global_lock"]["workers"][0].update(
                    completed_iterations=False, actual_writes=False, successful_locks=False,
                    successful_unlocks=False),
                lambda x: x["fallback_global_lock"]["workers"][0]["first_error"].update(iteration=False),
                lambda x: x["fallback_global_lock"]["workers"][0]["first_error"].__delitem__("rc"),
                # REV-218 RC2: deleting first_error from a non-injected worker
                # satisfies `worker.get("first_error") is not None` (a missing
                # key returns None) and the repaired copy then unconditionally
                # restamps first_error=None before validate_fallback ever
                # inspects it -- the same omission validate_fallback's F3
                # rejects on the original, and the drop-write branch already
                # rejects the identical omission.
                lambda x: [w.pop("first_error") for w in x["fallback_global_lock"]["workers"] if w["id"] != 0]):
            mutated = base()
            mutate(mutated)
            self.assertFalse(sup.control_oracle("fallback-lock-error", receipt, mutated, ""))

    def test_unexpected_fault_u1(self):
        r = {"returncode": 90, "timed_out": False, "result_sha256": None}
        self.assertTrue(sup.control_oracle("unexpected-fault", r, None, ""))
        for mutated in (dict(r, returncode=0), dict(r, returncode=-10), dict(r, timed_out=True),
                        dict(r, result_sha256="0"*64)):
            self.assertFalse(sup.control_oracle("unexpected-fault", mutated, None, ""))
        self.assertFalse(sup.control_oracle("unexpected-fault", r, {"completed_normal": True}, ""))
        self.assertFalse(sup.control_oracle("unexpected-fault", r, None, "MEET T2"))

    def test_watchdog_kills_and_reaps_exact_mock_child(self):
        child = Mock()
        child.wait.side_effect = [subprocess.TimeoutExpired("fixture", 5), -9]
        with tempfile.TemporaryDirectory(prefix="inv08-pure-watchdog-") as directory:
            with patch.object(sup.subprocess, "Popen", return_value=child) as spawn:
                r = sup.supervise(["synthetic-never-executed"], Path(directory)/"out", Path(directory)/"err", 5)
        self.assertTrue(r["timed_out"])
        self.assertEqual(r["returncode"], -9)
        child.kill.assert_called_once_with()
        self.assertEqual(child.wait.call_count, 2)
        spawn.assert_called_once()
        self.assertEqual(sup.MODES, {"normal":600, "negative":600, "fallback-only":60,
                                    "fallback-drop-write":60, "fallback-lock-error":60, "unexpected-fault":5})

    def test_v2_campaign_integrity_and_run_build_mixing(self):
        with tempfile.TemporaryDirectory(prefix="inv08-v2-fixture-") as directory:
            parent=Path(directory).resolve()
            packet,build,_,write=build_synthetic_campaign(parent)
            normal=parent/"normal/result.json"
            auth.validate_campaign(normal,build)
            self.assertEqual(auth.validate_result_binding(normal,auth.json_read(normal))["status"],"unqualified")
            for mode in packet["members"]:
                bad=copy.deepcopy(packet); bad["members"].pop(mode); write(parent/"campaign.json",bad)
                with self.assertRaises(ValueError): auth.validate_campaign(normal,build)
            write(parent/"campaign.json",packet)
            location=parent/"fallback-drop-write"
            original=auth.json_read(location/"result.json")
            def restamp(raw):
                write(location/"result.json",raw)
                receipt=auth.json_read(location/"result.supervisor.json")
                receipt["result_sha256"]=auth.file_sha(location/"result.json")
                write(location/"result.supervisor.json",receipt)
                manifest=auth.json_read(location/"manifest.json")
                for name in ("result.json","result.supervisor.json"):
                    manifest["artifacts"][name]=auth.file_sha(location/name)
                write(location/"manifest.json",manifest)
                packet["members"]["fallback-drop-write"]["sha256"]=auth.file_sha(location/"manifest.json")
                write(parent/"campaign.json",packet)
            for key,value in (("run_id","3"*32),("compiled_bundle_sha256","4"*64),("schema","INV-01-08-result-v2")):
                bad=copy.deepcopy(original); bad[key]=value; restamp(bad)
                with self.assertRaises(ValueError): auth.validate_campaign(normal,build)
            restamp(original)
            auth.validate_campaign(normal,build)
            (parent/"producing-build/build/litmus").write_text("tampered")
            with self.assertRaises(ValueError): auth.validate_result_binding(normal,auth.json_read(normal))

    def test_missing_clock_keeps_correctness_without_timing_claim(self):
        with patch.object(check,"validate_result_binding",return_value={"status":"unqualified"}), \
             patch.object(check,"json_read",return_value=valid()),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(check.cli(["synthetic"]),0)
        self.assertIn("TIMING UNQUALIFIED",output.getvalue())
        self.assertNotIn("MEET  T",output.getvalue())
        self.assertNotIn("MISS  T",output.getvalue())

    def test_watchdog_real_dummy_children(self):
        with tempfile.TemporaryDirectory(prefix="inv08-dummy-child-") as directory:
            root=Path(directory)
            good=sup.supervise([sys.executable,"-c","print('synthetic child')"],root/"out",root/"err",1)
            self.assertEqual(good["returncode"],0)
            self.assertFalse(good["timed_out"])
            timed=sup.supervise([sys.executable,"-c","import time; time.sleep(10)"],root/"out2",root/"err2",0.01)
            self.assertTrue(timed["timed_out"])
            self.assertLess(timed["returncode"],0)
            with self.assertRaises(FileExistsError):
                sup.supervise([sys.executable,"-c","pass"],root/"out",root/"err",1)

    def test_aggregate_dummy_success_timeout_tree_and_output(self):
        with tempfile.TemporaryDirectory(prefix="inv08-aggregate-") as directory:
            root=Path(directory)
            good=sup.supervise([sys.executable,"-c","print('synthetic')"],root/"out",root/"err",2,
                               output_root=root,max_bytes=1024*1024)
            self.assertEqual(good["returncode"],0)
            self.assertFalse(good["output_limited"])
            # Descendant keeps appending after its parent starts waiting. Killing
            # only the direct child would let this heartbeat continue growing.
            heartbeat=root/"heartbeat"
            program="import time; f=open(%r,'a'); [(f.write('x'),f.flush(),time.sleep(.01)) for _ in range(1000)]" % str(heartbeat)
            parent="import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',%r]); time.sleep(10)" % program
            timed=sup.supervise([sys.executable,"-c",parent],root/"out2",root/"err2",.3,
                                output_root=root,max_bytes=1024*1024)
            self.assertTrue(timed["timed_out"])
            self.assertLess(timed["returncode"],0)
            count=heartbeat.stat().st_size
            time.sleep(.1)
            self.assertEqual(heartbeat.stat().st_size,count)
            limited=sup.supervise([sys.executable,"-c","import os,time; os.write(1,b'x'*8192); time.sleep(10)"],
                                  root/"out3",root/"err3",2,output_root=root,max_bytes=4096)
            self.assertTrue(limited["output_limited"])
            self.assertLess(limited["returncode"],0)

    def test_aggregate_failure_retains_partial_schedule(self):
        with tempfile.TemporaryDirectory(prefix="inv08-retained-failure-") as directory:
            root=Path(directory).resolve()
            campaign=root/"campaign"
            def stopped(command, stdout, stderr, timeout, **kwargs):
                Path(stdout).write_text("synthetic partial build")
                Path(stderr).write_text("synthetic output limit")
                (campaign/"producing-build").mkdir()
                (campaign/"producing-build/partial").write_text("partial")
                return dict(returncode=-9,timed_out=False,output_limited=True,
                            watchdog_seconds=2700,output_limit_bytes=128*1024*1024)
            args=__import__("argparse").Namespace(directory=str(campaign),campaign_worker=False,
                                                 prepared_build=None,clock=None)
            with patch.dict(os.environ,NATEMU_RAW_ROOT=str(root)), patch.object(sup,"supervise",side_effect=stopped):
                self.assertEqual(sup.campaign(args),1)
            receipt=auth.json_read(campaign/"campaign.supervisor.json")
            self.assertEqual(receipt["status"],"failed")
            self.assertEqual(receipt["modes"],{mode:"not-run" for mode in sup.CAMPAIGN_MODES})
            self.assertIn("producing-build/partial",receipt["artifacts"])
            auth.verify_files(campaign,receipt["artifacts"])

    def test_missing_failed_outer_cannot_accept_campaign(self):
        with tempfile.TemporaryDirectory(prefix="inv08-outer-fixture-") as directory:
            root=Path(directory).resolve()
            _,build,_,write=build_synthetic_campaign(root)
            receipt=root/"campaign.supervisor.json"
            value=auth.json_read(receipt)
            for change in ({"status":"failed"},{"timed_out":True},{"output_limited":True},{"run_id":"9"*32}):
                write(receipt,dict(value,**change))
                with self.assertRaises(ValueError):
                    auth.validate_campaign(root/"normal/result.json",build)
            receipt.unlink()
            with self.assertRaises(OSError):
                auth.validate_campaign(root/"normal/result.json",build)

    def test_external_root_boundary(self):
        with tempfile.TemporaryDirectory(prefix="inv08-root-fixture-") as directory:
            root = Path(directory).resolve()
            self.assertEqual(auth.external(root/"result.json", root), root/"result.json")
            for destination, raw in ((auth.REPO/"bad.json", root), (root/"result.json", "/"),
                                     (root/"result.json", auth.REPO), (root, root)):
                with self.assertRaises(ValueError):
                    auth.external(destination, raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)

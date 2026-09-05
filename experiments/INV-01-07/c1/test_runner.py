"""Synthetic supervisor/gate tests. Popen is always mocked; no native launch.

Temporary files are synthetic fixtures, not raw evidence. These tests do not
establish host watchdog, direct-binary validation, native API/control behavior
or reviewer authenticity; those remain independent code/runtime obligations.
"""
import argparse
import contextlib
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest import mock

import core
import runner


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="c1-runner-synthetic-")
        self.base = Path(self.temporary.name).resolve()
        self.repo = self.base / "repository"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.raw = self.base / "synthetic-external"
        self.raw.mkdir()
        self.repo_patch = mock.patch.object(runner, "REPOSITORY", self.repo)
        self.repo_patch.start()

    def tearDown(self):
        self.repo_patch.stop()
        self.temporary.cleanup()

    def invalid(self, code, function, *args, **kwargs):
        with self.assertRaises(core.Invalid) as error:
            function(*args, **kwargs)
        self.assertEqual(str(error.exception), code)

    def put(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_frozen_schedule_and_literal_shuffle_vectors(self):
        self.assertEqual(runner.SCHEDULE, (
            (70701, "normal"), (70702, "normal"), (70703, "normal"),
            (70701, "backward"), (70701, "zero-nsec"), (70701, "omit-clock"),
            (70701, "omit-contention"), (70701, "pthread-error"), (70701, "global-inversion")))
        self.assertEqual(runner.CHILD_SECONDS, 300)
        # Independently generated with a separate uint32 LCG implementation;
        # these are literals, not runner.expected_order used as its own oracle.
        vectors = {
            70701: [13, 3, 5, 6, 11, 10, 0, 18, 1, 4, 19, 17, 7, 15, 14, 12, 2, 8, 9, 16],
            70702: [2, 18, 4, 5, 8, 7, 12, 13, 6, 10, 14, 19, 0, 11, 16, 9, 17, 15, 3, 1],
            70703: [1, 13, 3, 17, 7, 14, 9, 12, 18, 0, 5, 2, 15, 10, 4, 19, 11, 8, 16, 6],
        }
        for seed, expected in vectors.items():
            self.assertEqual(runner.expected_order(seed), expected)
        self.invalid("order-seed", runner.expected_order, True)

    def test_new_output_canonical_external_and_exclusive(self):
        self.assertEqual(runner.external_root(str(self.raw)), self.raw)
        self.invalid("missing-NATEMU_RAW_ROOT", runner.external_root, None)
        self.invalid("repository-internal-root", runner.external_root, str(self.repo))
        path = self.raw / "fresh"
        self.assertEqual(runner.new_destination(str(path), self.raw), path)
        path.mkdir()
        self.invalid("destination-exists", runner.new_destination, str(path), self.raw)
        link = self.raw / "alias"
        link.symlink_to(path, target_is_directory=True)
        self.invalid("destination-parent-canonical", runner.new_destination,
                     str(link / "new"), self.raw)
        self.invalid("destination-outside-root", runner.new_destination,
                     str(self.repo / "inside"), self.raw)
        output = self.raw / "unchanged.txt"
        runner.write_new(output, b"original")
        with self.assertRaises(FileExistsError):
            runner.write_new(output, b"replacement")
        self.assertEqual(output.read_bytes(), b"original")

    def test_launch_fd_binding_is_exact_and_bounded(self):
        output = self.raw / "child"
        expected = f"C1-LAUNCH-v1\n123\n70701\nnormal\n{output}\n".encode()
        self.assertEqual(runner.launch_message(output, 70701, "normal", 123), expected)
        self.invalid("launch-parent", runner.launch_message, output, 70701, "normal", True)
        self.invalid("launch-mode-seed", runner.launch_message, output, 1, "normal", 123)
        self.invalid("launch-mode-seed", runner.launch_message, output, 70701, "unknown", 123)
        self.invalid("launch-output", runner.launch_message, "/tmp/bad\npath", 70701, "normal", 123)
        self.invalid("launch-message-size", runner.launch_message, "/" + "a" * 4096, 70701, "normal", 123)

    def test_project_lock_excludes_other_runner_without_deleting_lock(self):
        path = self.raw / "synthetic.lock"
        with runner.CampaignLock(path):
            with self.assertRaises(BlockingIOError):
                with runner.CampaignLock(path):
                    self.fail("second lock admitted")
        with runner.CampaignLock(path):
            self.assertTrue(path.exists())

    def test_review_acceptance_independence_hash_and_receipt(self):
        text = "# Synthetic review, not evidence\n- Review ID: `REV-196`\n- Independence: `independent`\n- Verdict: `accept`.\n"
        data = text.encode()
        expected_hash = hashlib.sha256(data).hexdigest()
        with mock.patch.object(runner.artifact, "read_regular", return_value=data):
            self.assertEqual(runner.review_record("196", expected_hash, independent=True), text)
            self.invalid("review-independence", runner.review_record, "196", expected_hash, independent=False)
            self.invalid("review-hash", runner.review_record, "196", "0" * 64, independent=True)
        for bad_text, code in ((text.replace("`accept`", "`revise`"), "review-not-accepted"),
                               (text + "- Verdict: `accept`\n", "review-not-accepted"),
                               (text.replace("REV-196", "REV-195"), "review-id")):
            payload = bad_text.encode()
            with mock.patch.object(runner.artifact, "read_regular", return_value=payload):
                self.invalid(code, runner.review_record, "196", hashlib.sha256(payload).hexdigest(), independent=True)
        receipt = "- C1 source SHA-256: `" + "a" * 64 + "`\n"
        runner.receipt(receipt, "C1 source SHA-256", "a" * 64)
        self.invalid("review-receipt:C1 source SHA-256", runner.receipt,
                     receipt + receipt, "C1 source SHA-256", "a" * 64)

    def gate_fixture(self):
        build_dir = self.raw / "build"
        build_dir.mkdir()
        sdk = self.raw / "gate-sdk"
        sdk.mkdir()
        gate_path = self.raw / "gate.json"
        gate_path.write_text("{}")
        sources = {runner.SOURCE_PREFIX + "producer.c": "a" * 64,
                   runner.SOURCE_PREFIX + "runner.py": "b" * 64}
        source_digest = hashlib.sha256(json.dumps(sources, sort_keys=True, separators=(",", ":"),
                                                 ensure_ascii=True).encode()).hexdigest()
        gate = {"schema": runner.GATE_SCHEMA, "revision": "c" * 40, "source_files": sources,
                "method_sha256": "d" * 64, "reviews": {key: "e" * 64 for key in runner.REVIEWS},
                "build_directory": str(build_dir), "build_sha256": "f" * 64,
                "required_changes_closed": True}
        build = {"schema": runner.BUILD_SCHEMA, "source_files": sources, "source_revision": "1" * 40,
                 "files": {key: "a" * 64 for key in (
                     "clock_c1", "disassembly.stdout", "validation.stdout", "validation.stderr")},
                 "headers": {str(sdk / "usr/include" / name): "a" * 64 for name in (
                     "mach/mach_time.h", "time.h", "pthread.h", "pthread/qos.h", "sys/qos.h",
                     "CommonCrypto/CommonDigest.h")},
                 "commands": [], "sdk": str(sdk), "compiler": "synthetic",
                 "validation_exit": 0, "native_executed": False}
        text = (f"- C1 source SHA-256: `{source_digest}`\n"
                f"- C1 build SHA-256: `{'f' * 64}`\n- C1 method SHA-256: `{'d' * 64}`\n")
        return gate_path, gate, build, sources, text

    def gate_mocks(self, fixture):
        path, gate, build, sources, text = fixture
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(runner, "clean_scope"))
        stack.enter_context(mock.patch.object(runner, "revision", return_value="c" * 40))
        stack.enter_context(mock.patch.object(runner, "source_files", return_value=sources))
        stack.enter_context(mock.patch.object(runner, "review_record", return_value=text))
        stack.enter_context(mock.patch.object(runner.artifact, "read_json", side_effect=lambda p:
                                               gate if Path(p) == path else build))
        stack.enter_context(mock.patch.object(runner, "file_digest", side_effect=lambda p, *args:
                                               "f" * 64 if Path(p).name == "build.json" else
                                               "d" * 64 if Path(p).name == "INV-01-07.md" else "a" * 64))
        stack.enter_context(mock.patch.object(runner.os, "access", return_value=True))
        return stack

    def test_complete_gate_binds_sources_build_method_reviews_and_revision(self):
        fixture = self.gate_fixture()
        path, gate, build, sources, unused = fixture
        with self.gate_mocks(fixture):
            accepted, executable = runner.load_gate(str(path), self.raw)
            self.assertEqual(accepted, gate)
            self.assertEqual(executable, self.raw / "build/clock_c1")
            with mock.patch.object(runner, "revision", return_value="0" * 40):
                self.invalid("revision-changed", runner.load_gate, str(path), self.raw)
            with mock.patch.object(runner, "source_files", return_value={**sources, "unexpected": "a" * 64}):
                self.invalid("reviewed-source-mismatch", runner.load_gate, str(path), self.raw)
            build["native_executed"] = True
            self.invalid("build-status-or-source", runner.load_gate, str(path), self.raw)
            build["native_executed"] = False
            gate["required_changes_closed"] = False
            self.invalid("gate-status", runner.load_gate, str(path), self.raw)

    def test_dirty_or_uncommitted_scope_refuses(self):
        with mock.patch.object(runner, "git", return_value=" M experiments/INV-01-07/c1/core.py\0"):
            self.invalid("dirty-reviewed-scope", runner.clean_scope)
        with mock.patch.object(runner, "git", side_effect=["", "wrong-path"]):
            self.invalid("uncommitted-review-or-method", runner.clean_scope)

    def fake_launch(self, output, returncode=0, wait_effect=None):
        def launch(argv, **kwargs):
            self.assertEqual(argv[1], str(output))
            self.assertTrue(kwargs["start_new_session"])
            fd = int(kwargs["env"]["NATEMU_C1_LAUNCH_FD"])
            self.assertEqual(kwargs["pass_fds"], (fd,))
            message = os.read(fd, 4097)
            self.assertEqual(message, f"C1-LAUNCH-v1\n{os.getpid()}\n70701\nnormal\n{output}\n".encode())
            self.assertEqual(os.read(fd, 1), b"")
            output.mkdir()
            self.put(output / "record.json", {"synthetic": True})
            process = mock.Mock(pid=7654321, returncode=returncode)
            process.wait.return_value = returncode
            process.wait.side_effect = wait_effect
            process.poll.return_value = None
            return process
        return launch

    def test_supervisor_actual_completion_and_timeout_status(self):
        for name, returncode, effects, expected in (
                ("normal", 0, None, {"exit_status": 0, "signal": None, "timeout": False, "complete": True}),
                ("timeout", -9, [subprocess.TimeoutExpired("synthetic", 300), -9],
                 {"exit_status": None, "signal": 9, "timeout": True, "complete": False})):
            campaign = self.raw / name
            campaign.mkdir()
            output = campaign / "child"
            with (mock.patch.object(runner.subprocess, "Popen", side_effect=self.fake_launch(output, returncode, effects)) as launch,
                  mock.patch.object(runner.os, "killpg") as kill):
                actual = runner.supervise(Path("/not-executed"), output, 70701, "normal", campaign, self.raw)
            self.assertEqual(actual, expected)
            self.assertEqual(json.loads((output / "process.json").read_text()), expected)
            self.assertEqual(launch.call_count, 1)
            if name == "timeout":
                kill.assert_called_once_with(7654321, signal.SIGKILL)
            else:
                kill.assert_not_called()

    def test_spawn_failure_and_interrupt_retain_terminal_and_diagnostics(self):
        campaign = self.raw / "spawn"
        campaign.mkdir()
        output = campaign / "child"
        with mock.patch.object(runner.subprocess, "Popen", side_effect=OSError("synthetic spawn failure")):
            with self.assertRaises(OSError):
                runner.supervise(Path("/not-executed"), output, 70701, "normal", campaign, self.raw)
        self.assertEqual(json.loads((output / "process.json").read_text()), {
            "exit_status": None, "signal": None, "timeout": False, "complete": False})
        self.assertTrue((output / "supervisor-failure.json").is_file())
        campaign = self.raw / "interrupt"
        campaign.mkdir()
        output = campaign / "child"
        with mock.patch.object(runner.subprocess, "Popen", side_effect=self.fake_launch(
                output, -9, [KeyboardInterrupt(), -9])), mock.patch.object(runner.os, "killpg") as kill:
            with self.assertRaises(KeyboardInterrupt):
                runner.supervise(Path("/not-executed"), output, 70701, "normal", campaign, self.raw)
        kill.assert_called_once_with(7654321, signal.SIGKILL)
        self.assertEqual(json.loads((output / "process.json").read_text())["signal"], 9)

    def test_native_failure_control_requires_named_exact_target_and_process(self):
        directory = self.raw / "failure"
        directory.mkdir()
        terminal = {"exit_status": 2, "signal": None, "timeout": False, "complete": False}
        record = {"schema": runner.FAILURE_SCHEMA, "code": "pthread-lock", "api": "pthread_mutex_lock",
                  "status": errno.EINVAL, "mode": "pthread-error", "seed": 70701, "cell": 13, "worker": 0}
        self.put(directory / "failure.json", record)
        self.assertEqual(runner.failure_control(directory, "pthread-error", 70701, terminal)["observed"], "pthread-lock")
        self.invalid("control-wrong-process-status", runner.failure_control, directory, "pthread-error", 70701,
                     {**terminal, "timeout": True})
        record["worker"] = 1
        self.put(directory / "failure.json", record)
        self.invalid("control-wrong-pthread-injection", runner.failure_control,
                     directory, "pthread-error", 70701, terminal)
        record.update(code="nsec-zero", api="clock_gettime_nsec_np", mode="zero-nsec",
                      cell=1, worker=-1, status=errno.EINVAL)
        self.put(directory / "failure.json", record)
        self.assertEqual(runner.failure_control(directory, "zero-nsec", 70701, terminal)["observed"], "nsec-zero")
        record["status"] = 0
        self.put(directory / "failure.json", record)
        self.invalid("control-wrong-zero-nsec-status", runner.failure_control,
                     directory, "zero-nsec", 70701, terminal)

    def test_preflight_and_completed_resource_schema_actual_payload(self):
        directory = self.raw / "resources"
        directory.mkdir()
        preflight = {"schema": "INV-01-07-clock-c1-preflight-v1", "planned_payload_bytes": 226962048,
                     "maximum_owned_buffer_bytes": 40759456, "buffer_limit_bytes": 268435456,
                     "payload_limit_bytes": 536870912, "probe_stack_buffer_reserve_bytes": 1048576,
                     "named_stack_buffer_upper_bound_bytes": 200000}
        self.put(directory / "preflight.json", preflight)
        runner.resource_records(directory, "zero-nsec", complete=False)
        resources = {"schema": "INV-01-07-clock-c1-resources-v1", "peak_heap_global_buffer_bytes": 39710880,
                     "peak_owned_buffer_upper_bound_bytes": 40759456, "probe_stack_buffer_reserve_bytes": 1048576,
                     "array_payload_bytes": 226962048, "remaining_heap_global_buffer_bytes": 1310880}
        self.put(directory / "resources.json", resources)
        self.invalid("failure-with-completed-record", runner.resource_records,
                     directory, "zero-nsec", complete=False)
        for mode, delta in (("normal", 0), ("omit-clock", 16), ("omit-contention", 24)):
            resources["array_payload_bytes"] = 226962048 - delta
            self.put(directory / "resources.json", resources)
            # Mock filesystem inventory only. No226MB fixture or native execution.
            files = [mock.Mock() for _ in range(80)]
            for index, path in enumerate(files):
                path.is_file.return_value = True
                path.is_symlink.return_value = False
                path.stat.return_value = mock.Mock(st_size=226962048 - delta if index == 0 else 0)
            with mock.patch.object(Path, "glob", return_value=iter(files)):
                runner.resource_records(directory, mode, complete=True)
            with mock.patch.object(Path, "glob", return_value=iter(files[:-1])):
                self.invalid("resource-payload", runner.resource_records, directory, mode, complete=True)
        preflight["buffer_limit_bytes"] += 1
        self.put(directory / "preflight.json", preflight)
        self.invalid("resource-preflight", runner.resource_records, directory, "normal", complete=True)
        preflight["buffer_limit_bytes"] -= 1
        for bad_bound in (0, True, 1048577):
            preflight["named_stack_buffer_upper_bound_bytes"] = bad_bound
            self.put(directory / "preflight.json", preflight)
            self.invalid("resource-preflight", runner.resource_records, directory, "normal", complete=True)

    def test_public_errors_never_echo_exception_claim_text(self):
        poison = "T1: MEET RESULT: PASS findings STRUCTURAL_PASS secret-path"
        for error in (ValueError(poison), TypeError(poison), RuntimeError(poison), core.Invalid(poison), core.Unresolved(poison)):
            encoded = json.dumps(runner.public_error(error))
            for text in ("T1", "MEET", "PASS", "findings", "secret-path"):
                self.assertNotIn(text, encoded)
            with mock.patch.object(runner, "build", side_effect=error):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(runner.main(["build", str(self.raw / "unused")]), 1)
                for text in ("T1", "MEET", "PASS", "findings", "secret-path"):
                    self.assertNotIn(text, stderr.getvalue())

    def test_mutation_must_reach_named_oracle_not_just_mode_rejection(self):
        directory = self.raw / "synthetic-mutated"
        directory.mkdir()
        terminal = {"exit_status": 0, "signal": None, "timeout": False, "complete": True}
        for mode, code in (("backward", "clock-backward"), ("global-inversion", "global-trace-backward")):
            with (mock.patch.object(runner, "resource_records"), mock.patch.object(runner, "execution_order"),
                  mock.patch.object(runner.artifact, "read_json", return_value={"mode": mode, "seed": 70701}),
                  mock.patch.object(runner.artifact, "load_child", return_value=({}, {})),
                  mock.patch.object(runner.cells, "validate_child", side_effect=core.Invalid(code))):
                result = runner.inspect_child(directory, self.raw, 70701, mode, terminal)
                self.assertEqual(result["observed"], code)
                self.assertNotIn("findings", result)
                with mock.patch.object(runner.cells, "validate_child", side_effect=core.Invalid("injected-or-unknown-mode")):
                    self.invalid("unexpected-control-rejection", runner.inspect_child,
                                 directory, self.raw, 70701, mode, terminal)
                with mock.patch.object(runner.cells, "validate_child", return_value={}):
                    self.invalid("injection-not-detected", runner.inspect_child,
                                 directory, self.raw, 70701, mode, terminal)

    def test_build_pipeline_never_launches_producer(self):
        output = self.raw / "build-only"
        sdk = self.raw / "synthetic-sdk"
        sdk.mkdir()
        calls = []
        def command(argv, **unused):
            calls.append(argv)
            stdout = str(sdk).encode() + b"\n" if "--show-sdk-path" in argv else b"synthetic-tool-output\n"
            if "-o" in argv:
                Path(argv[argv.index("-o") + 1]).write_bytes(b"not-machine-code: synthetic build fixture")
            return subprocess.CompletedProcess(argv, 0, stdout, b"")
        with (mock.patch.dict(os.environ, {"NATEMU_RAW_ROOT": str(self.raw)}),
              mock.patch.object(runner, "review_record", return_value="accepted synthetic method"),
              mock.patch.object(runner, "source_files", return_value={"synthetic": "a" * 64}),
              mock.patch.object(runner, "git", return_value=""),
              mock.patch.object(runner, "revision", return_value="b" * 40),
              mock.patch.object(runner, "file_digest", return_value="a" * 64),
              mock.patch.object(runner, "command", side_effect=command),
              mock.patch.object(runner.subprocess, "Popen") as launch):
            self.assertEqual(runner.build(argparse.Namespace(output=str(output))), 0)
        launch.assert_not_called()
        self.assertFalse(any(argv[0] == str(output / "clock_c1") for argv in calls))
        self.assertTrue(any("unittest" in argv for argv in calls))
        self.assertTrue(any("otool" in argv for argv in calls))
        self.assertTrue(any("codesign" in argv[0] for argv in calls))
        result = json.loads((output / "build.json").read_text())
        self.assertIs(result["native_executed"], False)
        self.assertEqual(result["validation_exit"], 0)
        self.assertIn("clock_c1", result["files"])

    def test_omission_is_exactly_one_short_and_designated_cell(self):
        def record():
            return {"clock_cells": [{"qos": q, "clock": name, "readings": {"records": 200001}}
                                     for q in ("user-interactive", "background") for name in core.CLOCKS],
                    "contention_cells": [{"qos": q, "workers": n, "trace": {"records": n * 200000}}
                                          for q in ("user-interactive", "background") for n in (1, 2, 4, 8)]}
        value = record()
        value["clock_cells"][0]["readings"]["records"] = 200000
        self.assertEqual(runner.omitted_shape(value, "omit-clock"),
                         ("clock", "user-interactive", "mach_absolute_time"))
        value["clock_cells"][0]["readings"]["records"] = 199999
        self.invalid("control-wrong-omission-size", runner.omitted_shape, value, "omit-clock")
        value = record()
        value["contention_cells"][1]["trace"]["records"] = 399999
        self.assertEqual(runner.omitted_shape(value, "omit-contention"), ("contention", "user-interactive", 2))
        value["contention_cells"][0]["trace"]["records"] = 199999
        self.invalid("control-wrong-omission-target", runner.omitted_shape, value, "omit-contention")

    def test_order_requires_actual_exact_full_permutation(self):
        directory = self.raw / "order"
        directory.mkdir()
        order = [13, 3, 5, 6, 11, 10, 0, 18, 1, 4, 19, 17, 7, 15, 14, 12, 2, 8, 9, 16]
        record = {"schema": "INV-01-07-clock-c1-order-v1", "seed": 70701, "order": order}
        self.put(directory / "execution.json", record)
        runner.execution_order(directory, 70701)
        record["order"][0] = record["order"][1]
        self.put(directory / "execution.json", record)
        self.invalid("execution-order", runner.execution_order, directory, 70701)

    def test_whole_campaign_fixed_schedule_and_failure_no_retries(self):
        for fail_at, render_failure in ((None, False), (3, False), (3, True)):
            destination = self.raw / ("success" if fail_at is None else "render" if render_failure else "failure")
            args = argparse.Namespace(output=str(destination), gate=str(self.raw / "gate"),
                                      no_other_native_benchmarks=True)
            launched = []
            def supervise(exe, directory, seed, mode, campaign, root):
                launched.append((seed, mode))
                directory.mkdir()
                process = {"exit_status": 0, "signal": None, "timeout": False, "complete": True}
                self.put(directory / "process.json", process)
                return process
            def inspect(directory, root, seed, mode, terminal):
                if fail_at is not None and len(launched) - 1 == fail_at:
                    if render_failure:
                        return {"status": "EXPECTED_CONTROL_FAILURE", "late_unserializable": object()}
                    raise core.Invalid("T1: MEET RESULT: PASS findings STRUCTURAL_PASS")
                return {"status": "STRUCTURAL_PASS" if mode == "normal" else "EXPECTED_CONTROL_FAILURE",
                        "evidence_approval": False}
            snapshot = {key: {"stdout": key} for key in ("os", "uname", "hardware", "sdk")}
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, {"NATEMU_RAW_ROOT": str(self.raw)}))
                stack.enter_context(mock.patch.object(runner, "CampaignLock", return_value=contextlib.nullcontext()))
                stack.enter_context(mock.patch.object(runner, "load_gate", return_value=({"synthetic": True}, Path("/not-executed"))))
                stack.enter_context(mock.patch.object(runner, "file_digest", return_value="a" * 64))
                stack.enter_context(mock.patch.object(runner.shutil, "disk_usage", return_value=mock.Mock(free=10**12)))
                stack.enter_context(mock.patch.object(runner, "environment_snapshot", return_value=snapshot))
                stack.enter_context(mock.patch.object(runner, "supervise", side_effect=supervise))
                stack.enter_context(mock.patch.object(runner, "inspect_child", side_effect=inspect))
                if render_failure:
                    stack.enter_context(mock.patch.object(runner.check, "encode", side_effect=ValueError(
                        "T1: MEET RESULT: PASS findings STRUCTURAL_PASS")))
                self.assertEqual(runner.campaign(args), 0 if fail_at is None else 1)
            result = json.loads((destination / "campaign.json").read_text())
            self.assertIs(result["evidence_approval"], False)
            self.assertEqual(launched, list(runner.SCHEDULE[:len(launched)]))
            if fail_at is None:
                self.assertEqual(len(launched), 9)
                self.assertEqual(result["status"], "COMPLETE_STRUCTURAL_CAMPAIGN")
            else:
                self.assertEqual(len(launched), 4)
                self.assertEqual(result["status"], "INCOMPLETE")
                self.assertEqual(result["children"][3]["status"], "INVALID")
                self.assertEqual([r["status"] for r in result["children"]][4:], ["NOT_RUN"] * 5)
                summary = (destination / "campaign.json").read_text()
                failure = (destination / "campaign-failure.json").read_text()
                for text in ("T1:", "MEET", "PASS", "findings"):
                    self.assertNotIn(text, summary)
                    self.assertNotIn(text, failure)
                self.assertIn("T1: MEET", (destination / "campaign-error.diagnostic.txt").read_text())

    def test_no_operator_assertion_no_launch(self):
        args = argparse.Namespace(no_other_native_benchmarks=False)
        with mock.patch.object(runner.subprocess, "Popen") as launch:
            self.invalid("native-exclusivity-not-asserted", runner.campaign, args)
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)

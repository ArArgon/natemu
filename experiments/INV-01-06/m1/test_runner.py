"""Synthetic inputs and dummy Python children only; no Mach execution."""
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import check
import build
import run


class RunnerTests(unittest.TestCase):
    def test_idle_receiver_cannot_preempt_stall_watchdog(self):
        source=(run.HERE/'src/state_server.c').read_text()
        milliseconds=int(re.search(r'#define RECEIVE_TIMEOUT_MS (\d+)',source)[1])
        # Preserve the receive/parent-total ratio with a five-second dummy
        # ceiling; sub-200ms Python startup deadlines were flaky under compilation.
        # An idle second receiver starts before the delayed stall trigger.
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();binary=root/'dummy'
            idle=5*milliseconds/(run.CHILD_SECONDS*1000)
            binary.write_text('#!'+sys.executable+'\nimport os,time,threading\n'
                'def receiver():\n time.sleep('+repr(idle)+');os._exit(70)\n'
                'threading.Thread(target=receiver,daemon=True).start()\n'
                "print('started',flush=True)\ntime.sleep(.15)\nprint('stall-trigger',flush=True)\ntime.sleep(10)\n")
            binary.chmod(0o700)
            job=dict(kind='positive',seed=60601,warmup=0,checked=1,cell=dict(n=1,jit=0,access='load',topology='serial',path='direct'))
            with patch.object(run,'CHILD_SECONDS',5),patch.object(run,'NO_PROGRESS_SECONDS',1):
                run.run_child(binary,job,root/'child',dict(run_id='a'*32,build_id='b'*32,index=0))
            status=json.loads((root/'child/exit.json').read_text())
            self.assertEqual((status['returncode'],status['stop_reason']),(-9,'no-progress timeout'))
            self.assertIn('stall-trigger',(root/'child/trace.jsonl').read_text())

    def test_native_trace_oracles(self):
        check.demo()

    def test_no_progress_watchdog_retains_child_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();binary=root/"dummy"
            binary.write_text("#!"+sys.executable+"\nimport time\nprint('partial',flush=True)\ntime.sleep(10)\n")
            binary.chmod(0o700)
            job=dict(kind="positive",seed=60601,warmup=0,checked=1,
                     cell=dict(n=1,jit=0,access="load",topology="serial",path="direct"))
            with patch.object(run,"NO_PROGRESS_SECONDS",.5):
                result=run.run_child(binary,job,root/"child",dict(run_id="a"*32,build_id="b"*32,index=0))
            self.assertEqual(result["status"],"INVALID")
            status=json.loads((root/"child/exit.json").read_text())
            self.assertEqual(status["returncode"],-9)
            self.assertEqual(status["stop_reason"],"no-progress timeout")
            self.assertEqual((root/"child/trace.jsonl").read_text(),"partial\n")

    def test_snapshot_build_integrity_and_historical_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            build = root / "build"
            build.mkdir()
            run.snapshot_sources(build)
            self.assertEqual((build / "sources/src/state_server.c").read_bytes(),
                             (run.HERE / "src/state_server.c").read_bytes())
            (build / "state_server").write_bytes(b"synthetic")
            (build / "wire-layout.json").write_text("{}")
            for name in ("state_server-hardened","adhoc-plain-signature.txt","adhoc-hardened-allow-jit-signature.txt"):
                (build/name).write_bytes(b"synthetic")
            signing={"adhoc-plain":dict(binary="state_server",sha256=run.sha(build/"state_server")),
                     "adhoc-hardened-allow-jit":dict(binary="state_server-hardened",sha256=run.sha(build/"state_server-hardened"))}
            record = dict(signing=signing,schema="INV-01-06-m1-build-v3", build_id="a" * 32,
                          runtime_executed=False, files=run.file_manifest(build))
            def save():
                (build / "build-manifest.json").write_text(json.dumps(record))
            save()
            self.assertEqual(run.load_build(build, root)[0], record)
            (build / "state_server").write_bytes(b"mixed build")
            with self.assertRaisesRegex(ValueError, "checksum"):
                run.load_build(build, root)
            record.pop("schema")
            save()
            with self.assertRaisesRegex(ValueError, "historical"):
                run.load_build(build, root)
            with patch.dict(os.environ, {"NATEMU_RAW_ROOT": str(run.REPO)}):
                with self.assertRaises(SystemExit):
                    run.raw_root()

    def test_partial_source_copy_retains_failure_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            output = root / "partial"
            sdk = root / "sdk"
            sdk.mkdir()
            def copy_failure(directory):
                (directory / "partial.py").write_bytes(b"partial")
                raise OSError("synthetic copy failure")
            with (patch.dict(os.environ, {"NATEMU_RAW_ROOT": str(root)}),
                  patch.object(sys, "argv", ["build.py", "--output", str(output), "--sdk", str(sdk)]),
                  patch.object(build, "snapshot_sources", side_effect=copy_failure)):
                with self.assertRaises(OSError):
                    build.main()
            result = json.loads((output / "build-failure.json").read_text())
            self.assertTrue(result["hashing_complete"])
            self.assertEqual(result["files"]["partial.py"], run.sha(output / "partial.py"))

    def test_dummy_child_exit_input_and_control_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            binary = root / "dummy"
            job = dict(kind="positive", seed=60601, warmup=0, checked=1,
                       cell=dict(n=1, jit=0, access="load", topology="serial", path="direct"))
            identity = dict(run_id="a" * 32, build_id="b" * 32, index=0)
            trace = check._rows_to_text(check.good_trace_rows())
            def script(code=0, body=trace):
                binary.write_text("#!" + sys.executable + "\nimport sys\nsys.stdout.write(" + repr(body) +
                                  ")\nsys.exit(" + str(code) + ")\n")
                binary.chmod(0o700)
            script()
            self.assertEqual(run.run_child(binary, job, root / "good", identity)["status"], "checked")
            self.assertEqual(json.loads((root / "good/identity.json").read_text()), identity)
            with self.assertRaises(FileExistsError):
                run.run_child(binary, job, root / "good", identity)
            script(1)
            self.assertEqual(run.run_child(binary, job, root / "failed", identity)["status"], "INVALID")
            script(body=trace.replace('"seed": 60601', '"seed": 60602'))
            self.assertEqual(run.run_child(binary, job, root / "mixed", identity)["status"], "INVALID")
            script()
            control = dict(kind="control", seed=60601, arm=dict(arm="restoration"))
            self.assertEqual(run.run_child(binary, control, root / "control", identity)["status"], "INVALID")
            from trace_fixtures import valid_trace,held_trace
            rows=valid_trace(path="forward");rows[0]["arm"]="restoration"
            script(body=check._rows_to_text(rows))
            self.assertEqual(run.run_child(binary,control,root/"restored",identity)["status"],"checked")
            from test_controls import control_fixture
            rows,status=control_fixture("reply-send-failure")
            rows[0].update(n=2,topology="perworker")
            at=next(i for i,r in enumerate(rows) if r["kind"]=="arm")
            rows.insert(at,dict(kind="register",worker=1,recv_port=101,prior_recv_port=0,published=True))
            script(code=70,body=check._rows_to_text(rows))
            negative=dict(kind="negative",seed=60601,arm=dict(arm="reply-send-failure"))
            self.assertEqual(run.run_child(binary,negative,root/"negative",identity)["status"],"checked")
            rows,held=held_trace("server-before-repair")
            held.update(seed=60601,warmup=0,checked=1,cell=dict(n=2,jit=0,access="load",topology="perworker",path="direct"))
            script(body=check._rows_to_text(rows))
            self.assertEqual(run.run_child(binary,held,root/"held",identity)["status"],"checked")


if __name__ == "__main__":
    unittest.main()

"""Exercise the gate-relative outer watchdog with inert child processes."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import subprocess

PATH=Path(__file__).resolve().parents[1]/'experiments/track01/run_check.py'
spec=importlib.util.spec_from_file_location('containment_supervisor',PATH)
supervisor=importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)


class GateDeadlineTests(unittest.TestCase):
    def test_late_successful_exit_cannot_pass_after_slow_sampler(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);gate=root/'gate.json'
            gate.write_text(json.dumps(dict(host_pid=123,deadline_unix_ms=time.time()*1000+100)))
            def slow(pid,timeout):
                time.sleep(.25)
                return 1,[(pid,1)]
            code='import time; time.sleep(.15); print(\'{"status":"PASS"}\')'
            with patch.object(supervisor,'group_rss',slow):
                result=supervisor.supervise([sys.executable,'-c',code],root,
                    wall=5,period=.01,gate_file=gate)
            self.assertEqual(result['status'],'containment_outer_limit',result)
            self.assertEqual(result['returncode'],0)

    def test_sampler_timeout_at_deadline_retains_outer_limit_cause(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);gate=root/'gate.json'
            gate.write_text(json.dumps(dict(host_pid=123,deadline_unix_ms=time.time()*1000+100)))
            def exhausted(pid,timeout):
                time.sleep(timeout+.01)
                raise subprocess.TimeoutExpired('ps',timeout)
            with patch.object(supervisor,'group_rss',exhausted):
                result=supervisor.supervise([sys.executable,'-c','import time; time.sleep(30)'],root,
                    wall=5,period=.01,gate_file=gate)
            self.assertEqual(result['status'],'containment_outer_limit',result)
            self.assertLess(result['returncode'],0)

    def test_gate_deadline_excludes_startup_and_stops_hung_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);gate=root/'gate.json'
            code=('import json,os,pathlib,time; time.sleep(.3); '
                  'pathlib.Path("gate.json").write_text(json.dumps(dict(host_pid=os.getpid(),deadline_unix_ms=time.time()*1000+200))); '
                  'time.sleep(30)')
            result=supervisor.supervise([sys.executable,'-c',code],root,wall=5,period=.01,gate_file=gate)
            self.assertEqual(result['status'],'containment_outer_limit',result)
            self.assertLess(result['returncode'],0)
            self.assertGreaterEqual(result['elapsed_seconds'],.5)
            self.assertLess(result['elapsed_seconds'],2)

    def test_invalid_future_gate_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);gate=root/'gate.json'
            gate.write_text(json.dumps(dict(host_pid=123,deadline_unix_ms=time.time()*1000+60000)))
            result=supervisor.supervise([sys.executable,'-c','import time; time.sleep(30)'],root,
                wall=5,period=.01,gate_file=gate)
            self.assertEqual(result['status'],'supervisor_error')
            self.assertLess(result['returncode'],0)

if __name__=='__main__': unittest.main()

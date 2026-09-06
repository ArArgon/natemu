"""Small process-group and artifact fixtures; never execute timer code."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
import runner

class Supervision(unittest.TestCase):
    def run_dummy(self,source,wall=2,cap=1024*1024):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve()
            result,_=runner.supervise([sys.executable,'-c',source,str(root)],root,wall,cap)
            return result

    def test_success_and_failure(self):
        self.assertEqual(self.run_dummy('pass')['returncode'],0)
        self.assertEqual(self.run_dummy('raise SystemExit(7)')['returncode'],7)

    def test_timeout_kills_descendant(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve()
            source="import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',\"import time,sys;f=open(sys.argv[1],'a');exec('while True: f.write(\\\"x\\\"); f.flush(); time.sleep(.01)')\",sys.argv[1]+'/heartbeat']);time.sleep(30)"
            result,_=runner.supervise([sys.executable,'-c',source,str(root)],root,.25,1024*1024)
            self.assertTrue(result['timed_out']);size=(root/'heartbeat').stat().st_size
            time.sleep(.08);self.assertEqual(size,(root/'heartbeat').stat().st_size)

    def test_output_stops_group(self):
        result=self.run_dummy("import sys,time;f=open(sys.argv[1]+'/large','wb');f.write(b'x'*8192);f.flush();time.sleep(30)",cap=4096)
        self.assertTrue(result['output_limited'])

    def test_missing_failed_receipt_and_artifact_mutation(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve()
            with self.assertRaises(FileNotFoundError):runner.validate_campaign(root)
            runner.write(root/'supervisor.json',dict(schema='M07-T2-supervisor-v1',status='failed'))
            runner.write(root/'schedule.json',{})
            with self.assertRaises(AssertionError):runner.validate_campaign(root)
            (root/'source').write_text('original');inventory=runner.files(root);runner.verify(root,inventory)
            (root/'source').write_text('changed')
            with self.assertRaises(AssertionError):runner.verify(root,inventory)
            with self.assertRaises(AssertionError):runner.verify(root,{'../source':'bad'})

if __name__=='__main__':unittest.main()

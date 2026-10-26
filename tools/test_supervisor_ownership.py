"""Inert-child tests for supervisor group ownership; no VM/compiler execution."""
import errno
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'experiments/track01/run_check.py'
spec = importlib.util.spec_from_file_location('ownership_supervisor', PATH)
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)


class ChildOwnershipTests(unittest.TestCase):
    def child(self, code):
        child = subprocess.Popen([sys.executable, '-c', code], start_new_session=True)
        def cleanup():
            if child.returncode is None:
                try:os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:pass
                finally:child.wait(timeout=3)
        self.addCleanup(cleanup)
        return child

    def test_repeated_peeks_leave_exited_child_available_for_final_reap(self):
        child = self.child('raise SystemExit(23)')
        deadline = time.monotonic() + 3
        while supervisor.peek_child(child) is None:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        self.assertEqual(supervisor.peek_child(child), 23)
        self.assertEqual(supervisor.peek_child(child), 23)
        self.assertIsNone(child.returncode)
        self.assertEqual(child.wait(timeout=1), 23)
        with self.assertRaises(ChildProcessError):supervisor.peek_child(child)

    def test_supervisor_signals_owned_group_before_sole_final_reap(self):
        original_kill = os.killpg; original_wait = subprocess.Popen.wait; original_poll = subprocess.Popen.poll
        actions = []
        def kill(pid, sig):
            actions.append(('signal', pid));return original_kill(pid, sig)
        def poll(child):
            if child.args[0] == '/bin/ps':return original_poll(child)
            raise AssertionError('reaping monitored child poll')
        def wait(child, timeout=None):
            if child.args[0] == '/bin/ps':return original_wait(child, timeout=timeout)
            self.assertEqual(actions, [('signal', child.pid)])
            self.assertIsNone(child.returncode)
            actions.append(('reap', child.pid))
            return original_wait(child, timeout=timeout)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(supervisor, 'group_rss', side_effect=lambda pid, timeout:(1, [(pid, 1)])), \
                    patch.object(supervisor.os, 'killpg', side_effect=kill), \
                    patch.object(supervisor.subprocess.Popen, 'poll', new=poll), \
                    patch.object(supervisor.subprocess.Popen, 'wait', new=wait):
                result = supervisor.supervise([sys.executable, '-c', 'print(\'{"status":"PASS"}\')'],
                    Path(temporary), wall=3, period=.01)
        self.assertEqual(result['status'], 'passed', result)
        self.assertTrue(result['process_reaped'])
        self.assertTrue(result.get('group_signalled') or result.get('group_quiescent'))
        self.assertEqual([a[0] for a in actions], ['signal', 'reap'])

    def test_permission_denial_cannot_hide_live_group_member(self):
        child = self.child('pass')
        while supervisor.peek_child(child) is None:time.sleep(.005)
        output=f'{child.pid} {child.pid} Z\n{child.pid+1} {child.pid} S\n'
        with patch.object(supervisor.subprocess,'check_output',return_value=output):
            with self.assertRaisesRegex(RuntimeError,'live owned-group'):supervisor.exited_group_members(child,1)
        with patch.object(supervisor.subprocess,'check_output',return_value=f'{child.pid} {child.pid} Z\n'):
            self.assertEqual(supervisor.exited_group_members(child,1),
                [dict(pid=child.pid,pgid=child.pid,state='Z')])
        self.assertIsNone(child.returncode)
        child.wait(timeout=1)

    def test_echild_never_signals_reused_group_or_claims_reap(self):
        child = self.child('pass')
        child.wait(timeout=3)
        # Simulate an uncoordinated reap: Popen has not retained the receipt.
        child.returncode = None
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(supervisor.subprocess, 'Popen', return_value=child), \
                    patch.object(supervisor.os, 'killpg', side_effect=AssertionError('signal after lost lease')):
                result = supervisor.supervise(['inert-already-reaped-child'], Path(temporary), wall=3)
        child.returncode = 0
        self.assertEqual(result['status'], 'supervisor_error', result)
        self.assertFalse(result['process_reaped'])
        self.assertIn('lease_error', result)
        self.assertNotIn('group_signalled', result)

    def test_lost_lease_does_not_replace_existing_limit_cause(self):
        child = self.child('pass');child.wait(timeout=3)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(supervisor.subprocess, 'Popen', return_value=child), \
                    patch.object(supervisor.os, 'killpg', side_effect=AssertionError('unowned signal')):
                result = supervisor.supervise(['inert-already-reaped-child'], Path(temporary), wall=0)
        self.assertEqual(result['status'], 'wall_limit', result)
        self.assertFalse(result['process_reaped'])
        self.assertIn('lease_error', result)


if __name__ == '__main__':unittest.main()

"""The framework's own tests stay apart from the machine they run on (test_pool.py stubs outside_vms itself)."""
import os


def test_framework_tests_ignore_the_machines_real_vms(monkeypatch):
    from types import SimpleNamespace
    from testkit.sched import pool as p
    assert os.environ.get('TESTKIT_IGNORE_OUTSIDE_VMS') == '1'           # set for every framework test (conftest)
    called = []
    monkeypatch.setattr(p.subprocess, 'run', lambda *a, **k: called.append(a) or SimpleNamespace(stdout='123\n'))
    assert p.outside_vms() == [] and not called
    monkeypatch.delenv('TESTKIT_IGNORE_OUTSIDE_VMS')
    assert p.outside_vms() == ['123'] * len(p.VM_CHECKS)

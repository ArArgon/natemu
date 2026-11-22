import os
import subprocess
import sys

from testkit import paths

COMMANDS = ('run', 'submit', 'status', 'wait', 'lock', 'check-lock', 'prepare', 'gc')


def test_help_lists_commands():
    r = subprocess.run([sys.executable, '-m', 'testkit', '--help'], capture_output=True, text=True,
                       cwd=paths.repo_root(), env=dict(os.environ, PYTHONPATH=str(paths.repo_root())))
    assert r.returncode == 0, r.stderr
    for c in COMMANDS:
        assert c in r.stdout


def test_paths_default_and_override(monkeypatch, tmp_path):
    monkeypatch.delenv('NATEMU_TEST_CACHE', raising=False)
    assert str(paths.cache_root()).endswith('Library/Caches/natemu/testkit')
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path))
    assert paths.cache_root() == tmp_path
    assert paths.state_dir() == tmp_path / 'state'


def test_a_recording_run_is_never_a_gate(monkeypatch):
    """TESTKIT_SEED_ANSWERS or TESTKIT_SEED_FAILURES with --gate is refused before anything runs (run, submit, prepare):
    such a run reports what it records as unverified, and a gate of it could bless a regression."""
    import pytest
    from testkit import cli
    for v in cli.RECORDING: monkeypatch.delenv(v, raising=False)
    assert cli.with_default_tier(['--gate']) == ['--gate']
    for v in cli.RECORDING:
        monkeypatch.setenv(v, '1')
        with pytest.raises(SystemExit, match=f'{v} set: a recording run is never a gate'):
            cli.with_default_tier(['--gate'])
        assert cli.with_default_tier(['--tier', 'campaign']) == ['--tier', 'campaign']       # recording itself is allowed
        monkeypatch.delenv(v)

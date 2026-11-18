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

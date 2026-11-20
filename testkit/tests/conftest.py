import pathlib
import shutil

import pytest

pytest_plugins = ['pytester']
FIXTURES = pathlib.Path(__file__).parent / 'fixtures'


@pytest.fixture
def family_dir(pytester):
    """Copy a fixture family into a pytester folder as tests/<name>/; return a function doing that."""
    def make(name):
        dest = pytester.path / 'tests' / name
        shutil.copytree(FIXTURES / name, dest)
        return dest
    return make


collect_ignore = ['fixtures']       # fixture families are run by pytester, not collected here


@pytest.fixture(autouse=True)
def _isolated_raw_root(monkeypatch, tmp_path_factory):
    """Never touch the machine's real natemu-raw (its .vm.lock) from the framework's own tests."""
    monkeypatch.setenv('NATEMU_RAW_ROOT', str(tmp_path_factory.mktemp('raw')))
    monkeypatch.delenv('NATEMU_STATE_ROOT', raising=False)
    # Nor wait for the machine's real VMs: a private pool's exclusive window would never open while other runs boot VMs
    # (seen: test_two_runs_share_machine hung for 15 minutes in four checkouts at once).
    monkeypatch.setenv('TESTKIT_IGNORE_OUTSIDE_VMS', '1')
    # Nor measure the efficiency cores' slowdown for every private state folder (testkit.sched.speed; test_speed.py unsets it)
    monkeypatch.setenv('NATEMU_TEST_E_SLOWDOWN', '3')


@pytest.fixture(autouse=True)
def _local_builder(monkeypatch):
    """The machine's own config.toml may send container builds to an x86 box ([build] builder): the framework's own
    tests stub `container`, so they build 'local' unless a test chooses otherwise."""
    monkeypatch.setenv('NATEMU_CONTAINER_BUILDER', 'local')

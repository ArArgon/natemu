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

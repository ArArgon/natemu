"""The efficiency cores' slowdown: measured once per machine and macOS build, cached, never measured on a busy machine."""
import dataclasses
import json

import pytest

from testkit.host import Host
from testkit.sched import pool, speed

HOST = Host(machine_id='M-1', chip='Apple M2 Max', p_cores=8, e_cores=4, os_build='25G1', features={})


@pytest.fixture
def measured(monkeypatch):
    monkeypatch.delenv('NATEMU_TEST_E_SLOWDOWN', raising=False)
    calls = []
    monkeypatch.setattr(speed, 'measure', lambda: calls.append(1) or (3.2, {'p_rate': 32.0, 'e_rate': 10.0, 'raw': 3.2}))
    return calls


def test_measured_once_then_cached(tmp_path, measured):
    assert speed.e_slowdown(tmp_path, HOST, lambda: True) == 3.2
    assert speed.e_slowdown(tmp_path, HOST, lambda: True) == 3.2
    assert len(measured) == 1
    rec = json.loads((tmp_path / speed.FILE).read_text())
    assert rec['machine_id'] == 'M-1' and rec['os_build'] == '25G1' and rec['chip'] == 'Apple M2 Max'


def test_a_busy_machine_uses_the_default_and_measures_later(tmp_path, measured):
    assert speed.e_slowdown(tmp_path, HOST, lambda: False) == speed.DEFAULT
    assert not measured and not (tmp_path / speed.FILE).exists()
    assert speed.e_slowdown(tmp_path, HOST, lambda: True) == 3.2


def test_another_machine_or_macos_build_measures_again(tmp_path, measured):
    speed.e_slowdown(tmp_path, HOST, lambda: True)
    speed.e_slowdown(tmp_path, dataclasses.replace(HOST, os_build='25H7'), lambda: True)
    speed.e_slowdown(tmp_path, dataclasses.replace(HOST, machine_id='M-2'), lambda: True)
    assert len(measured) == 3


def test_override_and_no_efficiency_cores(tmp_path, measured, monkeypatch):
    assert speed.e_slowdown(tmp_path, dataclasses.replace(HOST, e_cores=0), lambda: True) == 1.0
    monkeypatch.setenv('NATEMU_TEST_E_SLOWDOWN', '1')
    assert speed.e_slowdown(tmp_path, HOST, lambda: True) == 1.0
    assert not measured


def test_idle_means_an_empty_pool_and_a_low_load(tmp_path, monkeypatch):
    monkeypatch.setattr(pool.os, 'getloadavg', lambda: (1.0, 1.0, 1.0))
    assert pool._idle(tmp_path, 8)
    (tmp_path / 'pool.json').write_text(json.dumps({'held': [{'request': {}}], 'waiting': []}))
    assert not pool._idle(tmp_path, 8)
    (tmp_path / 'pool.json').write_text(json.dumps({'held': [], 'waiting': []}))
    monkeypatch.setattr(pool.os, 'getloadavg', lambda: (6.0, 1.0, 1.0))
    assert not pool._idle(tmp_path, 8)


def test_a_real_measurement_finds_the_efficiency_cores_slower():
    """On this Mac, briefly (it is what the framework does once per machine)."""
    if not pool.host.detect().e_cores: pytest.skip('no efficiency cores')
    value, d = speed.measure(rounds=1, seconds=0.3)
    assert speed.LIMITS[0] <= value <= speed.LIMITS[1] and d['p_rate'] > 0 and d['e_rate'] > 0


def test_a_failed_or_implausible_measurement_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.delenv('NATEMU_TEST_E_SLOWDOWN', raising=False)
    def broken(): raise OSError('taskpolicy: not found')
    monkeypatch.setattr(speed, 'measure', broken)
    assert speed.e_slowdown(tmp_path, HOST, lambda: True) == speed.DEFAULT
    monkeypatch.setattr(speed, 'measure', lambda: (1.1, {'p_rate': 11.0, 'e_rate': 10.0, 'raw': 1.1}))   # both on E
    assert speed.e_slowdown(tmp_path, HOST, lambda: True) == speed.DEFAULT
    assert speed.cached(tmp_path, HOST) is None


def test_a_failed_measurement_is_retried_only_after_an_hour(tmp_path, monkeypatch):
    monkeypatch.delenv('NATEMU_TEST_E_SLOWDOWN', raising=False)
    calls = []
    monkeypatch.setattr(speed, 'measure', lambda: calls.append(1) or (1.1, {'p_rate': 11.0, 'e_rate': 10.0, 'raw': 1.1}))
    for _ in range(3): assert speed.e_slowdown(tmp_path, HOST, lambda: True) == speed.DEFAULT
    assert len(calls) == 1
    rec = json.loads((tmp_path / speed.FILE).read_text()); rec['at'] -= speed.RETRY + 1
    (tmp_path / speed.FILE).write_text(json.dumps(rec))
    speed.e_slowdown(tmp_path, HOST, lambda: True)
    assert len(calls) == 2


def test_work_starting_during_the_measurement_discards_it(tmp_path, measured):
    answers = iter([True, False])
    assert speed.e_slowdown(tmp_path, HOST, lambda: next(answers)) == speed.DEFAULT
    assert not (tmp_path / speed.FILE).exists()


def test_a_damaged_cache_is_measured_again(tmp_path, measured):
    (tmp_path / speed.FILE).write_text(json.dumps({'machine_id': 'M-1', 'os_build': '25G1', 'value': 'x'}))
    assert speed.e_slowdown(tmp_path, HOST, lambda: True) == 3.2 and len(measured) == 1


def test_a_bad_override_is_an_error(tmp_path, monkeypatch):
    for v in ('0', 'fast', 'nan', '0.5'):
        monkeypatch.setenv('NATEMU_TEST_E_SLOWDOWN', v)
        with pytest.raises(ValueError, match='NATEMU_TEST_E_SLOWDOWN'): speed.e_slowdown(tmp_path, HOST, lambda: True)

import multiprocessing as mp
import os
import signal
import time

import pytest

from testkit import cache


@pytest.fixture(autouse=True)
def root(monkeypatch, tmp_path):
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    return tmp_path / 'c'


def _slow_build(counter):
    def build(d):
        with open(counter, 'a') as f: f.write('x')
        time.sleep(0.5)
        (d / 'out').write_text('done')
    return build


def _worker(counter, q):
    q.put(str(cache.get_or_build('x', 'k1', _slow_build(counter))))


def test_build_once_concurrently(tmp_path):
    counter = tmp_path / 'count'
    q = mp.Queue()
    ps = [mp.Process(target=_worker, args=(counter, q)) for _ in range(2)]
    for p in ps: p.start()
    for p in ps: p.join(10)
    paths = {q.get(timeout=1), q.get(timeout=1)}
    assert len(paths) == 1
    assert counter.read_text() == 'x'
    assert (cache.entry_path('x', 'k1') / 'out').read_text() == 'done'


def _hang_build(d):
    (d / 'partial').write_text('half')
    time.sleep(60)


def _hanger():
    cache.get_or_build('x', 'k2', _hang_build)


def test_killed_builder_never_served(root):
    p = mp.Process(target=_hanger); p.start()
    deadline = time.time() + 5
    while not list((root / 'x').glob('.tmp-k2-*')) and time.time() < deadline: time.sleep(0.05)
    os.kill(p.pid, signal.SIGKILL); p.join()
    assert not cache.entry_path('x', 'k2').exists()
    got = cache.get_or_build('x', 'k2', lambda d: (d / 'out').write_text('ok'))
    assert (got / 'out').read_text() == 'ok' and not (got / 'partial').exists()
    assert not list((root / 'x').glob('.tmp-k2-*'))


def _sleeper():
    time.sleep(60)


def test_gc_respects_leases_and_roots(root):
    big = b'0' * 100_000
    es = [cache.get_or_build('x', f'e{i}', lambda d: (d / 'f').write_bytes(big)) for i in range(4)]
    holder = mp.Process(target=_sleeper); holder.start()
    try:
        cache.lease('run-live', [es[0]], pid=holder.pid)
        dead = mp.Process(target=_sleeper); dead.start(); dead.kill(); dead.join()
        cache.lease('run-dead', [es[1]], pid=dead.pid)
        deleted = cache.gc(cap_bytes=150_000, roots={es[2]})
        assert es[0].exists() and es[2].exists()
        assert set(deleted) <= {es[1], es[3]} and deleted
        assert not any(e.exists() for e in deleted)
    finally:
        holder.kill(); holder.join()


def test_key_directory_stable(tmp_path):
    for top in ('a', 'b'):
        (tmp_path / top / 'sub').mkdir(parents=True)
        (tmp_path / top / 'sub' / 'f').write_text('hello')
        (tmp_path / top / 'g').write_text('world')
    assert cache.key(tmp_path / 'a') == cache.key(tmp_path / 'b')
    (tmp_path / 'b' / 'g').write_text('worle')
    assert cache.key(tmp_path / 'a') != cache.key(tmp_path / 'b')
    assert cache.key('a', 'b') != cache.key('ab')


def test_cap_parse(monkeypatch):
    monkeypatch.setenv('NATEMU_TEST_CACHE_CAP', '2G')
    assert cache.cap_bytes() == 2 << 30
    monkeypatch.delenv('NATEMU_TEST_CACHE_CAP')
    assert cache.cap_bytes() == 100 << 30


def test_peek_raises_instead_of_building():
    with pytest.raises(cache.Miss):
        with cache.peek():
            cache.get_or_build('x', 'p1', lambda d: (d / 'f').write_text('built'))
    assert not cache.entry_path('x', 'p1').exists()
    e = cache.get_or_build('x', 'p1', lambda d: (d / 'f').write_text('built'))
    with cache.peek():
        assert cache.get_or_build('x', 'p1', lambda d: None) == e

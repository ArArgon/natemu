import multiprocessing as mp
import os
import signal
import subprocess
import sys
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


def test_gc_keeps_the_disk_reserve_free_under_a_large_cap(root, monkeypatch):
    big = b'0' * 100_000
    es = [cache.get_or_build('x', f'e{i}', lambda d: (d / 'f').write_bytes(big)) for i in range(4)]
    monkeypatch.setattr(cache, 'free_bytes', lambda p: 50_000)
    deleted = cache.gc(cap_bytes=100 << 30, roots={es[3]}, reserve=200_000)
    assert len(deleted) >= 2 and es[3].exists()                 # 150 kB had to go so that 200 kB stays free
    assert not any(e.exists() for e in deleted)


def test_gc_deletes_nothing_with_room_on_disk_and_under_the_cap(root, monkeypatch):
    es = [cache.get_or_build('x', f'e{i}', lambda d: (d / 'f').write_bytes(b'0' * 100_000)) for i in range(2)]
    monkeypatch.setattr(cache, 'free_bytes', lambda p: 50 << 30)
    assert cache.gc(cap_bytes=100 << 30, roots=set(), reserve=10 << 30) == [] and all(e.exists() for e in es)


def test_reserve_parse(monkeypatch):
    monkeypatch.setenv('NATEMU_TEST_DISK_RESERVE', '3G')
    assert cache.reserve_bytes() == 3 << 30
    monkeypatch.delenv('NATEMU_TEST_DISK_RESERVE')
    assert cache.reserve_bytes() == 10 << 30


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


def _is_apfs(path) -> bool:
    df = subprocess.run(['df', '-P', str(path)], capture_output=True, text=True).stdout.strip().splitlines()
    if len(df) < 2: return False
    mount = df[-1].split(None, 5)[-1]
    out = subprocess.run(['diskutil', 'info', '-plist', mount], capture_output=True, text=True)
    return 'apfs' in out.stdout.lower()


def test_clone_shares_blocks_on_apfs(tmp_path):
    if not _is_apfs(tmp_path): pytest.skip('not an APFS volume')
    src = tmp_path / 'big'
    src.write_bytes(os.urandom(1 << 20) * 64)
    os.sync()
    before = os.statvfs(tmp_path).f_bavail * os.statvfs(tmp_path).f_frsize
    cache.clone(src, tmp_path / 'copy')
    os.sync()
    after = os.statvfs(tmp_path).f_bavail * os.statvfs(tmp_path).f_frsize
    assert (tmp_path / 'copy').read_bytes() == src.read_bytes()
    assert before - after < (1 << 20)


def test_clone_falls_back_to_copy(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cache, '_clone_warned', False)
    def fail(cmd, *a, **k): raise subprocess.CalledProcessError(1, cmd)
    monkeypatch.setattr(cache.subprocess, 'run', fail)
    src = tmp_path / 'f'; src.write_bytes(b'abc' * 1000)
    cache.clone(src, tmp_path / 'g')
    assert (tmp_path / 'g').read_bytes() == src.read_bytes()
    d = tmp_path / 'd'; (d / 'sub').mkdir(parents=True); (d / 'sub/x').write_text('x'); os.symlink('sub/x', d / 'link')
    cache.clone(d, tmp_path / 'e')
    assert os.readlink(tmp_path / 'e/link') == 'sub/x'
    assert capsys.readouterr().err.count('copied') == 1      # said once per process


def test_clone_folder_keeps_symlinks(tmp_path):
    d = tmp_path / 'd'; (d / 'sub').mkdir(parents=True); (d / 'sub/x').write_text('x')
    os.symlink('sub/x', d / 'link'); os.symlink('/nonexistent', d / 'dangling')
    cache.clone(d, tmp_path / 'e')
    assert os.path.islink(tmp_path / 'e/link') and os.readlink(tmp_path / 'e/link') == 'sub/x'
    assert os.path.islink(tmp_path / 'e/dangling')
    assert (tmp_path / 'e/sub/x').read_text() == 'x'

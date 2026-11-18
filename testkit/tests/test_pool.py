import fcntl
import json
import multiprocessing as mp
import os
import time
import uuid

from testkit.sched.policy import Config, Request
from testkit.sched.pool import Pool, vm_lock

CFG = Config(p_cores=4, e_cores=0, window_budget=600, min_shared=0)


def _claimer(state_dir, log, n):
    pool = Pool(state_dir, CFG)
    for _ in range(n):
        r = Request(id=uuid.uuid4().hex, owner_pid=os.getpid(), cores=2, perf=True, expected=0.2)
        pool.submit(r)
        while not pool.poll(r.id): time.sleep(0.01)
        t0 = time.time(); time.sleep(0.2); t1 = time.time()
        pool.release(r.id)
        with open(log, 'a') as f: f.write(json.dumps([t0, t1, 2]) + '\n')


def test_two_processes_share_budget(tmp_path):
    log = tmp_path / 'log'
    ps = [mp.Process(target=_claimer, args=(tmp_path, log, 3)) for _ in range(3)]
    for p in ps: p.start()
    for p in ps: p.join(30)
    iv = [json.loads(l) for l in log.read_text().splitlines()]
    assert len(iv) == 9
    for t0, t1, _ in iv:
        mid = (t0 + t1) / 2
        assert sum(c for a, b, c in iv if a <= mid < b) <= 4


def _holder(state_dir, q):
    pool = Pool(state_dir, CFG)
    r = Request(id='holder', owner_pid=os.getpid(), cores=4, perf=True)
    pool.submit(r)
    while not pool.poll('holder'): time.sleep(0.01)
    q.put('held'); time.sleep(60)


def test_dead_owner_released(tmp_path):
    q = mp.Queue()
    p = mp.Process(target=_holder, args=(tmp_path, q)); p.start()
    assert q.get(timeout=10) == 'held'
    pool = Pool(tmp_path, CFG)
    r = Request(id='me', owner_pid=os.getpid(), cores=4, perf=True)
    pool.submit(r)
    assert pool.poll('me') is None
    p.kill(); p.join()
    deadline = time.time() + 1
    while not pool.poll('me'): assert time.time() < deadline; time.sleep(0.01)


def test_waiting_reason_names_blocker(tmp_path):
    pool = Pool(tmp_path, CFG)
    x = Request(id='x', owner_pid=os.getpid(), exclusive=True, label='l4b', worktree='pcid-fix', expected=100)
    pool.submit(x); assert pool.poll('x')
    s = Request(id='s', owner_pid=os.getpid(), cores=1)
    pool.submit(s); assert pool.poll('s') is None
    reason = pool.waiting_reason('s')
    assert 'l4b' in reason and 'pcid-fix' in reason and 's left' in reason


def test_vm_lock_interop(tmp_path):
    lockf = tmp_path / '.vm.lock'
    fd = os.open(lockf, os.O_RDWR | os.O_CREAT); fcntl.flock(fd, fcntl.LOCK_EX)       # an old runner holds it
    pool = Pool(tmp_path, CFG)
    x = Request(id='x', owner_pid=os.getpid(), exclusive=True)
    pool.submit(x); g = pool.poll('x')
    got = []
    ctx = mp.get_context('fork')

    def take():
        with vm_lock(g, lockf): os._exit(0)
    p = ctx.Process(target=take); p.start()
    time.sleep(0.5); assert p.is_alive()        # the window waits for the old runner
    fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)
    p.join(5); assert p.exitcode == 0


import pytest as _pytest


@_pytest.fixture(autouse=True)
def _no_outside_vms(monkeypatch):
    """The machine's real VM processes are not these tests' business."""
    from testkit.sched import pool as p
    monkeypatch.setattr(p, 'outside_vms', lambda: [])

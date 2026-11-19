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



def test_pool_file_stays_readable_by_older_code(tmp_path):
    """pool.json is shared with other checkouts' (older) code, which builds Request(**d) from each stored request."""
    import json as _json
    from testkit.sched.policy import Request as R
    pool = Pool(tmp_path, CFG)
    pool.submit(R(id='a', owner_pid=os.getpid(), run='run-1'))
    raw = _json.loads((tmp_path / 'pool.json').read_text())
    old_fields = {'id', 'owner_pid', 'cores', 'exclusive', 'perf', 'priority', 'asked', 'expected', 'label', 'worktree'}
    for d in raw['waiting'] + [g['request'] for g in raw['held']]:
        assert set(d) <= old_fields
    assert raw['runs'] == {'a': 'run-1'}
    assert pool.snapshot().waiting[0].run == 'run-1'


def test_a_finished_grant_counts_for_its_run_until_the_run_is_done(tmp_path):
    from testkit.sched.policy import granted  # noqa: F401
    from testkit.sched.policy import Request as R
    pool = Pool(tmp_path, CFG)
    pool.submit(R(id='a', owner_pid=os.getpid(), run='r')); pool.submit(R(id='b', owner_pid=os.getpid(), run='r'))
    assert pool.poll_many(['a', 'b'])
    time.sleep(0.05); pool.release('a')
    assert pool.snapshot().served.get('r', 0) > 0
    pool.release('b')
    assert pool.snapshot().served.get('r', 0) > 0       # kept for a grace period (a run between prepare and its units)
    import testkit.sched.pool as poolmod
    old = poolmod.KEEP_SHARE; poolmod.KEEP_SHARE = 0.0
    try: assert 'r' not in pool.snapshot().served      # then forgotten
    finally: poolmod.KEEP_SHARE = old


def test_a_grant_blocked_on_the_old_lock_goes_back_to_the_queue(tmp_path, monkeypatch):
    """runloop: the pool opens no window while the old lock is held, but an old runner blocked on it can take it between
    the grant and the start. A start that cannot take natemu-raw/.vm.lock for LOCK_GIVE_BACK seconds releases the grant
    and queues again with its ask time, instead of holding the machine empty (an exclusive grant blocks everything)."""
    import types
    from testkit import runloop
    from testkit.sched.policy import Request as R
    lock = tmp_path / '.vm.lock'; lock.touch()
    monkeypatch.setattr(runloop.poolmod, 'vm_lock_file', lambda: lock)
    monkeypatch.setattr('testkit.sched.pool.vm_lock_file', lambda: lock)
    monkeypatch.setattr('testkit.sched.pool.outside_vms', lambda: [])
    monkeypatch.setattr(runloop, 'LOCK_GIVE_BACK', 0.0)
    pool = Pool(tmp_path, CFG)
    req = R(id='x', owner_pid=os.getpid(), exclusive=True, asked=1.0, run='r')
    pool.submit(req)
    grant = pool.poll('x')
    assert grant is not None and grant.core_class == 'X'
    holder = os.open(lock, os.O_RDWR); fcntl.flock(holder, fcntl.LOCK_EX)       # an old runner takes it before the start
    ctl = types.SimpleNamespace(pool=pool)
    unit = runloop.Unit(0, [], req, {}, 60)
    assert runloop.Controller.start(ctl, unit, grant) is False
    st = pool.snapshot()
    assert not st.held and [r.id for r in st.waiting] == ['x'] and st.waiting[0].asked == 1.0
    fcntl.flock(holder, fcntl.LOCK_UN); os.close(holder)

#!/usr/bin/env python3
"""Run a command under the shared VM lock, for anything that starts a VM outside run_suite (which takes it itself).

    python3 tools/vmlock.py shared|exclusive COMMAND [ARG ...]      lock: NATEMU_STATE_ROOT/.vm.lock (default NATEMU_RAW_ROOT)

First it queues in the test framework's scheduler (testkit.sched.pool, the machine's one queue; `tools/natemu-test
status` shows it) as a run of its own: exclusive for `exclusive`, one core for `shared`. So VMs started this way wait
their turn in order with every framework run instead of racing it for the lock. Once granted it takes the lock
without blocking; if an old runner (run_suite, `lockf -k`) holds it for 10 s, it gives the grant back unused and
queues again, so it never holds the machine while waiting for something outside the queue. Inside a framework unit
(NATEMU_VM_LOCK_HELD=1 or a framework worker) the framework already holds both: the command just runs. Without the
framework (no testkit, unreadable pool) it takes the lock alone, as it always did.

The one lock primitive is BSD flock(2) on that file: Python's fcntl.flock, the suite, the I/O runners and `/usr/bin/lockf -k`
(macOS lockf is flock-based and exclusive only) all exclude each other. fcntl.lockf / F_SETLK record locks do NOT: never use them
on this file. Shared: many parallel, correctness-only VM runs. Exclusive: timing-sensitive runs and benchmarks (what
`lockf -k` takes).

The grant is held by this process id, which the command keeps (exec), so it lasts until the command exits; the
scheduler then drops it as it drops any grant whose owner has died."""
import fcntl, os, pathlib, sys, time, uuid
from raw_root import STATE_ROOT

REPO = pathlib.Path(__file__).resolve().parent.parent
GIVE_BACK = 10.0


def framework():
    """(Pool, Request class) of the test framework's scheduler, or None where it cannot be used."""
    try:
        sys.path.insert(0, str(REPO))
        from testkit.sched.policy import INTERACTIVE, Request
        from testkit.sched.pool import Pool
        pool = Pool(); pool.snapshot()                 # reads the state once: a broken pool falls back here
        return pool, Request, INTERACTIVE
    except Exception as e:
        print(f'vmlock: not queuing in the test framework ({type(e).__name__}: {e}); taking the lock only', file=sys.stderr)
        return None


def take(fd: int, mode: str, blocking: bool) -> bool:
    try:
        fcntl.flock(fd, (fcntl.LOCK_SH if mode == 'shared' else fcntl.LOCK_EX) | (0 if blocking else fcntl.LOCK_NB))
        return True
    except BlockingIOError:
        return False


def main():
    assert len(sys.argv) > 2 and sys.argv[1] in ('shared', 'exclusive'), __doc__
    mode, command = sys.argv[1], sys.argv[2:]
    if os.environ.get('NATEMU_VM_LOCK_HELD') == '1' or os.environ.get('TESTKIT_CONTROLLER_PID'):
        os.execvp(command[0], command)                 # a framework unit: its controller holds the claim and the lock
    fd = os.open(STATE_ROOT / '.vm.lock', os.O_RDWR | os.O_CREAT, 0o644)
    fw = framework()
    if fw is None:
        take(fd, mode, blocking=True)
    else:
        pool, Request, INTERACTIVE = fw
        run = f'vmlock-{uuid.uuid4().hex[:8]}'
        req = Request(id=f'{run}:{uuid.uuid4().hex}', owner_pid=os.getpid(), cores=1, exclusive=mode == 'exclusive',
                      priority=INTERACTIVE, asked=time.time(), worktree=REPO.name, run=run,
                      label='vmlock: ' + ' '.join(pathlib.Path(c).name for c in command[:3])[:60])
        said = None
        try:
            pool.submit(req)
            while True:
                while not pool.poll(req.id):
                    why = pool.waiting_reason(req.id)
                    if why != said: print(f'vmlock: waiting: {why}', file=sys.stderr, flush=True); said = why
                    time.sleep(0.5)
                since = time.time()
                while not take(fd, mode, blocking=False) and time.time() - since < GIVE_BACK: time.sleep(0.2)
                if take(fd, mode, blocking=False): break
                print('vmlock: an old runner holds the lock: grant given back, queued again', file=sys.stderr, flush=True)
                pool.release(req.id, account=False); pool.submit(req)
        except Exception as e:                         # the queue failed mid-way: the lock alone, as before
            print(f'vmlock: the test framework queue failed ({type(e).__name__}: {e}); taking the lock only', file=sys.stderr)
            try: pool.release(req.id, account=False)
            except Exception: pass
            take(fd, mode, blocking=True)
    os.set_inheritable(fd, True)
    os.execvp(command[0], command)   # the command inherits the locked descriptor, so the lock lasts until it (and its children) exit


if __name__ == '__main__': main()

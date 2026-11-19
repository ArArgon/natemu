"""The machine's claim pool (spec R4): one flock'd JSON state per machine, shared by every run from every worktree.
Decisions come from testkit.sched.policy; the pool only stores state, prunes requests whose owner died, and answers
why a request waits. Interop with the old runners (R4.7, R4.9): `vm_lock(grant)` takes natemu-raw/.vm.lock shared for
shared VM work and exclusively for a window, and `outside_vms()` lists VM processes the framework did not start."""
import contextlib
import dataclasses
import fcntl
import json
import os
import pathlib
import subprocess
import time

from testkit import host, paths
from testkit.sched.policy import (Config, Grant, Request, State, cost, decide, granted, order_key, run_of, shares,
                                  unsatisfiable, window_pending)

KEEP_SHARE = 600.0      # s a run's share and starvation clock outlive its last request (between prepare and its units)


def default_config() -> Config:
    if v := os.environ.get('NATEMU_TEST_CORES'):          # "P,E": a test or a deliberately smaller budget
        p, e = (int(x) for x in v.split(','))
    else:
        h = host.detect(); p, e = h.p_cores, h.e_cores
    budget, min_shared = (float(x) for x in os.environ.get('NATEMU_TEST_WINDOW', '600,120').split(','))
    return Config(p_cores=p, e_cores=e, window_budget=budget, min_shared=min_shared)


_FIELDS = {f.name for f in dataclasses.fields(Request)}


OUTSIDE = ('run', 'light')         # Request fields stored outside the request dicts


def _request(d: dict, runs: dict, light=()) -> Request:
    """A stored request. pool.json is shared with older checkouts' code, which builds Request(**d): new fields are kept
    outside the request dicts (`runs`, `light`), and fields this code does not know are ignored."""
    r = Request(**{k: v for k, v in d.items() if k in _FIELDS and k not in OUTSIDE})
    r.run = runs.get(r.id, d.get('run', ''))
    r.light = r.id in light
    return r


def _stored(r: Request) -> dict:
    d = dataclasses.asdict(r)
    for k in OUTSIDE: d.pop(k, None)
    return d


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0); return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Pool:
    def __init__(self, state_dir: pathlib.Path | None = None, cfg: Config | None = None):
        self.dir = state_dir or paths.state_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cfg = cfg or default_config()

    @contextlib.contextmanager
    def _state(self):
        fd = os.open(self.dir / 'pool.lock', os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            f = self.dir / 'pool.json'
            raw = json.loads(f.read_text()) if f.exists() else {}
            runs, light = raw.get('runs', {}), set(raw.get('light', []))
            st = State(held=[Grant(_request(g['request'], runs, light), g['start'], g['core_class']) for g in raw.get('held', [])],
                       waiting=[_request(r, runs, light) for r in raw.get('waiting', [])],
                       window_open_since=raw.get('window_open_since'), last_window_closed=raw.get('last_window_closed'),
                       served=raw.get('served', {}), last_grant=raw.get('last_grant', {}))
            for g in [g for g in st.held if not _alive(g.request.owner_pid)]:
                self._account(st, g)                # its owner died: the time it held still counts for its run
            st.held = [g for g in st.held if _alive(g.request.owner_pid)]
            st.waiting = [r for r in st.waiting if _alive(r.owner_pid)]
            yield st
            live = {run_of(g.request) for g in st.held} | {run_of(r) for r in st.waiting}
            now = time.time()                                # a finished run's share is forgotten, after a grace period
            keep = lambda k: k in live or now - st.last_grant.get(k, 0.0) < KEEP_SHARE
            st.served = {k: v for k, v in st.served.items() if keep(k)}
            st.last_grant = {k: v for k, v in st.last_grant.items() if keep(k)}
            tmp = self.dir / f'pool.json.{os.getpid()}'
            tmp.write_text(json.dumps({'held': [{'request': _stored(g.request), 'start': g.start, 'core_class': g.core_class}
                                                for g in st.held],
                                       'waiting': [_stored(r) for r in st.waiting],
                                       'runs': {r.id: r.run for r in [g.request for g in st.held] + st.waiting if r.run},
                                       'light': [r.id for r in [g.request for g in st.held] + st.waiting if r.light],
                                       'window_open_since': st.window_open_since,
                                       'last_window_closed': st.last_window_closed, 'served': st.served,
                                       'last_grant': st.last_grant}))
            os.replace(tmp, f)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)

    def _account(self, st: State, g: Grant) -> None:
        st.served[run_of(g.request)] = st.served.get(run_of(g.request), 0.0) + max(0.0, time.time() - g.start) * cost(g, self.cfg)

    def submit(self, req: Request) -> None:
        with self._state() as st:
            if not any(r.id == req.id for r in st.waiting) and not any(g.request.id == req.id for g in st.held):
                st.waiting.append(req)

    def poll(self, req_id: str) -> Grant | None:
        return self.poll_many([req_id]).get(req_id)

    def poll_many(self, ids: list[str]) -> dict:
        """One decision for the whole machine; the grants (held) among `ids`."""
        wanted = set(ids)
        now = time.time()
        with self._state() as st:
            busy = False
            if not any(g.core_class == 'X' for g in st.held) and (
                    (window_pending(st, self.cfg, now) and not any(g.core_class != 'L' for g in st.held))
                    or st.window_open_since is not None):
                busy = bool(external_busy())         # an open window also closes while an old runner holds the lock
            self.last_external = busy
            d = decide(st, self.cfg, now, external_busy=busy)
            st.window_open_since, st.last_window_closed = d.window_open_since, d.last_window_closed
            for r, cls in d.grants:
                st.waiting = [w for w in st.waiting if w.id != r.id]
                st.held.append(Grant(r, now, cls)); granted(st, r, now)
            return {g.request.id: g for g in st.held if g.request.id in wanted}

    def release(self, req_id: str, account: bool = True) -> None:
        """Drop a request; a held grant's time counts for its run unless `account` is False (a grant given back unused)."""
        with self._state() as st:
            if account:
                for g in [g for g in st.held if g.request.id == req_id]: self._account(st, g)
            st.held = [g for g in st.held if g.request.id != req_id]
            st.waiting = [r for r in st.waiting if r.id != req_id]

    def snapshot(self) -> State:
        with self._state() as st:
            return st

    def unsatisfiable(self, req: Request) -> bool:
        return unsatisfiable(req, self.cfg)

    def waiting_reason(self, req_id: str) -> str:
        now = time.time()
        with self._state() as st:
            me = next((r for r in st.waiting if r.id == req_id), None)
            if me is None: return 'running' if any(g.request.id == req_id for g in st.held) else 'not queued'
            if getattr(self, 'last_external', False):
                return f'window waits for the machine: {external_busy()}'
            x = next((g for g in st.held if g.core_class == 'X'), None)
            if x:
                left = f', ~{max(0, x.start + x.request.expected - now):.0f} s left' if x.request.expected else ''
                return f'exclusive {x.request.label} from {x.request.worktree or "?"} running{left}'
            share = shares(st, self.cfg, now)
            ahead = sorted((r for r in st.waiting if r.exclusive), key=lambda r: order_key(r, share, st.last_grant, self.cfg, now))
            if me.exclusive:
                pos = [r.id for r in ahead].index(me.id)
                return f'exclusive queue: {pos} ahead; machine draining' if pos else 'exclusive queue: next; machine draining'
            if window_pending(st, self.cfg, now) and ahead:
                return f'window pending: {len(ahead)} exclusive test(s) ahead ({ahead[0].label} from {ahead[0].worktree or "?"})'
            used = sum(g.request.cores for g in st.held if g.core_class == 'P')
            return f'waiting for cores: {used}/{self.cfg.p_cores} performance cores in use'


def raw_root() -> pathlib.Path | None:
    """The old runners' folder (tools/raw_root.py's rule), when it exists on this machine."""
    try:
        common = subprocess.run(['git', '-C', str(paths.repo_root()), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                                capture_output=True, text=True, check=True).stdout.strip()
        root = pathlib.Path(os.environ.get('NATEMU_RAW_ROOT') or pathlib.Path(common).parent.parent / 'natemu-raw')
    except (OSError, subprocess.CalledProcessError):
        return None
    return root if root.is_dir() else None


def vm_lock_file() -> pathlib.Path | None:
    """The old runners' VM lock (tools/raw_root.py STATE_ROOT): in $NATEMU_STATE_ROOT, else in the raw root."""
    s = os.environ.get('NATEMU_STATE_ROOT')
    root = pathlib.Path(s) if s else raw_root()
    return root / '.vm.lock' if root is not None and root.is_dir() else None


@contextlib.contextmanager
def vm_lock(grant: Grant, lock_file: pathlib.Path | None = None):
    """Hold the old runners' VM lock for this grant: exclusively for a window, shared otherwise. No-op without natemu-raw."""
    f = lock_file or vm_lock_file()
    if f is None:
        yield; return
    fd = os.open(f, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if grant.core_class == 'X' else fcntl.LOCK_SH); yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


VM_CHECKS = (['pgrep', '-x', 'natemu-host'], ['pgrep', '-x', 'natemu'], ['pgrep', '-f', '^[^ ]*qemu-system'])


def outside_vms() -> list[str]:
    """natemu and QEMU VM processes running now (by process name, as tools/run_suite.py:idle checks). The framework's own
    tests set TESTKIT_IGNORE_OUTSIDE_VMS: their private pools must not wait for the machine's real VMs."""
    if os.environ.get('TESTKIT_IGNORE_OUTSIDE_VMS'): return []
    found = []
    for c in VM_CHECKS:
        r = subprocess.run(c, capture_output=True, text=True)
        found += r.stdout.split()
    return found


def external_busy(lock_file: pathlib.Path | None = None) -> str:
    """Why a window cannot open now, or '' (R4.7, R4.9): an old runner holds natemu-raw/.vm.lock, or VMs run that the
    framework did not start (an exclusive window opens only on a drained machine, so any VM process is outside it)."""
    f = lock_file or vm_lock_file()
    if f is not None and f.exists():
        fd = os.open(f, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            return f'{f} is held by another runner'
        finally:
            os.close(fd)
    vms = outside_vms()
    return f'VM processes running outside the framework: {" ".join(vms)}' if vms else ''

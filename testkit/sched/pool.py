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
from testkit.sched.policy import Config, Grant, Request, State, decide, unsatisfiable, window_pending


def default_config() -> Config:
    if v := os.environ.get('NATEMU_TEST_CORES'):          # "P,E": a test or a deliberately smaller budget
        p, e = (int(x) for x in v.split(','))
    else:
        h = host.detect(); p, e = h.p_cores, h.e_cores
    budget, min_shared = (float(x) for x in os.environ.get('NATEMU_TEST_WINDOW', '600,120').split(','))
    return Config(p_cores=p, e_cores=e, window_budget=budget, min_shared=min_shared)


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
            st = State(held=[Grant(Request(**g['request']), g['start'], g['core_class']) for g in raw.get('held', [])],
                       waiting=[Request(**r) for r in raw.get('waiting', [])],
                       window_open_since=raw.get('window_open_since'), last_window_closed=raw.get('last_window_closed'))
            st.held = [g for g in st.held if _alive(g.request.owner_pid)]
            st.waiting = [r for r in st.waiting if _alive(r.owner_pid)]
            yield st
            tmp = self.dir / f'pool.json.{os.getpid()}'
            tmp.write_text(json.dumps({'held': [dataclasses.asdict(g) for g in st.held],
                                       'waiting': [dataclasses.asdict(r) for r in st.waiting],
                                       'window_open_since': st.window_open_since,
                                       'last_window_closed': st.last_window_closed}))
            os.replace(tmp, f)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)

    def submit(self, req: Request) -> None:
        with self._state() as st:
            if not any(r.id == req.id for r in st.waiting) and not any(g.request.id == req.id for g in st.held):
                st.waiting.append(req)

    def poll(self, req_id: str) -> Grant | None:
        now = time.time()
        with self._state() as st:
            busy = False
            if window_pending(st, self.cfg, now) and not any(g.core_class != 'X' for g in st.held) \
                    and not any(g.core_class == 'X' for g in st.held):
                busy = bool(external_busy())
            self.last_external = busy
            d = decide(st, self.cfg, now, external_busy=busy)
            st.window_open_since, st.last_window_closed = d.window_open_since, d.last_window_closed
            for r, cls in d.grants:
                st.waiting = [w for w in st.waiting if w.id != r.id]
                st.held.append(Grant(r, now, cls))
            return next((g for g in st.held if g.request.id == req_id), None)

    def release(self, req_id: str) -> None:
        with self._state() as st:
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
            ahead = sorted((r for r in st.waiting if r.exclusive), key=lambda r: (-r.priority, r.asked))
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


@contextlib.contextmanager
def vm_lock(grant: Grant, lock_file: pathlib.Path | None = None):
    """Hold the old runners' VM lock for this grant: exclusively for a window, shared otherwise. No-op without natemu-raw."""
    f = lock_file or ((raw_root() / '.vm.lock') if raw_root() else None)
    if f is None:
        yield; return
    fd = os.open(f, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if grant.core_class == 'X' else fcntl.LOCK_SH); yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


VM_CHECKS = (['pgrep', '-x', 'natemu-host'], ['pgrep', '-x', 'natemu'], ['pgrep', '-f', '^[^ ]*qemu-system'])


def outside_vms() -> list[str]:
    """natemu and QEMU VM processes running now (by process name, as tools/run_suite.py:idle checks)."""
    found = []
    for c in VM_CHECKS:
        r = subprocess.run(c, capture_output=True, text=True)
        found += r.stdout.split()
    return found


def external_busy(lock_file: pathlib.Path | None = None) -> str:
    """Why a window cannot open now, or '' (R4.7, R4.9): an old runner holds natemu-raw/.vm.lock, or VMs run that the
    framework did not start (an exclusive window opens only on a drained machine, so any VM process is outside it)."""
    f = lock_file or ((raw_root() / '.vm.lock') if raw_root() else None)
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

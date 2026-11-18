"""The run loop (spec R4, R7): pytest collects, this decides when and where items run.

The controller groups runnable CaseItems into units (family, suite, variant; up to `batch` items), prepares what they
need (testkit.prepare), submits one claim per unit to the machine pool, and starts each granted unit as a worker
subprocess (`python -m pytest --testkit-worker IDS`). Workers write serialized reports to a JSON-lines file, which the
controller replays into its own session, so pytest's terminal output, JUnit and exit status work unchanged. Items that
will not run (excluded, not runnable, stale lock, unpreparable) are reported by the controller itself.
"""
import contextlib
import fcntl
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
import uuid

import pytest
from _pytest.reports import TestReport

from testkit import durations, paths
from testkit.collect import CaseItem
from testkit.sched import pool as poolmod
from testkit.sched.policy import GATE, INTERACTIVE, Request

POLL = 0.1
NO_RAW_CACHES = {'NATEMU_REF_CACHE': 'off', 'NATEMU_ASM_CACHE': 'off', 'NATEMU_QEMU_CACHE': 'off'}


class Unit:
    def __init__(self, n, items, req, env, timeout):
        self.n, self.items, self.req, self.env, self.timeout = n, items, req, env, timeout
        self.proc = self.grant = self.started = self.vmlock = None
        self.offset = 0
        self.done = set()


def run_dir(config) -> pathlib.Path:
    d = os.environ.get('TESTKIT_RUN_DIR')
    p = pathlib.Path(d) if d else paths.runs_dir() / f'adhoc-{time.strftime("%Y%m%d-%H%M%S")}-{uuid.uuid4().hex[:4]}'
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cores(n, cfg):
    return cfg.p_cores if n == 'all' else int(n)


def build_units(items, config, pool) -> tuple[list[Unit], list[tuple[CaseItem, str, str]]]:
    """Units in longest-first order, and items reported without running: (item, category, reason)."""
    table = durations.load()
    groups, order = {}, []
    for it in items:
        if it.unit_key not in groups: groups[it.unit_key] = []; order.append(it.unit_key)
        groups[it.unit_key].append(it)
    prio = GATE if (config.getoption('priority') == 'gate' or (config.getoption('gate') and not config.getoption('priority'))) \
        else INTERACTIVE
    units, refused = [], []
    for k in order:
        g = groups[k]
        s = g[0].suite
        for i in range(0, len(g), max(1, s.batch)):
            chunk = g[i:i + s.batch]
            exp = [table.get(it.nodeid) for it in chunk]
            req = Request(id=uuid.uuid4().hex, owner_pid=os.getpid(), cores=_cores(s.needs.cores, pool.cfg),
                          exclusive=s.needs.exclusive, perf=s.needs.perf or s.needs.dedicated, priority=prio,
                          asked=time.time(), expected=None if None in exp else sum(exp),
                          label=f'{g[0].fam.name}/{s.name}' + (f'[{g[0].variant.id}]' if g[0].variant.params else ''),
                          worktree=paths.repo_root().name)
            if pool.unsatisfiable(req):
                refused += [(it, 'not-runnable', f'needs {req.cores} cores; this machine has {pool.cfg.p_cores} '
                                                 f'performance and {pool.cfg.e_cores} efficiency cores') for it in chunk]
                continue
            env = dict(g[0].place.env)
            if s.kind != 'legacy': env.update(NO_RAW_CACHES)
            units.append(Unit(len(units), chunk, req, env, float(s.params.get('timeout', 3600))))
    units.sort(key=lambda u: -(u.req.expected if u.req.expected is not None else 1e9))
    return units, refused


def report_without_running(item, category, reason):
    item.user_properties.append(('testkit', category))
    item.add_marker(pytest.mark.skip(reason=f'{category}: {reason}'))
    item.ihook.pytest_runtest_protocol(item=item, nextitem=None)


class Controller:
    def __init__(self, session):
        self.session, self.config = session, session.config
        self.dir = run_dir(self.config) / 'units'
        self.dir.mkdir(parents=True, exist_ok=True)
        self.pool = poolmod.Pool()
        self.reasons = {}

    def run(self) -> None:
        items = self.session.items
        plain = [i for i in items if not isinstance(i, CaseItem) or i.preset]
        for i in plain:
            i.ihook.pytest_runtest_protocol(item=i, nextitem=None)
        live = [i for i in items if isinstance(i, CaseItem) and not i.preset]
        from testkit import prepare
        failed = prepare.prepare(live, self.pool, self.config)
        for it in live:
            if it.nodeid in failed: report_without_running(it, *failed[it.nodeid])
        live = [i for i in live if i.nodeid not in failed]
        units, refused = build_units(live, self.config, self.pool)
        for it, c, r in refused: report_without_running(it, c, r)
        for u in units: self.pool.submit(u.req)
        waiting, running = list(units), []
        try:
            while waiting or running:
                if waiting:
                    grants = self.pool.poll_many([u.req.id for u in waiting])
                    for u in [u for u in waiting if u.req.id in grants]:
                        if self.start(u, grants[u.req.id]):
                            waiting.remove(u); running.append(u)
                for u in list(running):
                    self.tail(u)
                    if u.proc.poll() is not None or time.time() - u.started > u.timeout:
                        self.finish(u); running.remove(u)
                self.show_waiting(waiting, running)
                time.sleep(POLL)
        finally:
            for u in running: self.kill(u)
            for u in waiting + running: self.pool.release(u.req.id)

    def start(self, u: Unit, grant) -> bool:
        lock = poolmod.raw_root()
        if lock is not None:
            fd = os.open(lock / '.vm.lock', os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if grant.core_class == 'X' else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd); return False          # an old runner holds it: keep the grant, try again next poll
            u.vmlock = fd
        u.grant = grant
        ids = self.dir / f'{u.n}.ids'
        ids.write_text('\n'.join(i.nodeid for i in u.items) + '\n')
        files = sorted({str(i.path) for i in u.items})
        passthrough = [a for o in ('fresh', 'diff') if self.config.getoption(o) for a in (f'--{o}',)]
        passthrough += [a for f in self.config.getoption('require_real') or () for a in ('--require-real', f)]
        cmd = ([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '--rootdir', str(self.config.rootpath)]
               + (['-c', str(self.config.inipath)] if self.config.inipath else ['-p', 'testkit.plugin'])
               + ['--testkit-worker', str(ids), '--testkit-report', str(self.dir / f'{u.n}.jsonl')] + passthrough + files)
        if grant.core_class == 'E': cmd = ['taskpolicy', '-c', 'background'] + cmd
        env = dict(os.environ, **u.env, TESTKIT_CONTROLLER_PID=str(os.getpid()), TESTKIT_CORE_CLASS=grant.core_class)
        log = open(self.dir / f'{u.n}.log', 'wb')
        u.proc = subprocess.Popen(cmd, cwd=self.config.rootpath, env=env, stdout=log, stderr=subprocess.STDOUT,
                                  start_new_session=True)
        u.started = time.time()
        return True

    def tail(self, u: Unit) -> None:
        f = self.dir / f'{u.n}.jsonl'
        if not f.exists(): return
        with open(f) as fh:
            fh.seek(u.offset)
            while (line := fh.readline()) and line.endswith('\n'):
                u.offset = fh.tell()
                self.replay(u, json.loads(line))

    def replay(self, u: Unit, ev: dict) -> None:
        hook = self.config.hook
        if ev['kind'] == 'start':
            hook.pytest_runtest_logstart(nodeid=ev['nodeid'], location=tuple(ev['location']))
        elif ev['kind'] == 'report':
            rep = hook.pytest_report_from_serializable(config=self.config, data=ev['data'])
            if isinstance(rep.longrepr, list): rep.longrepr = tuple(rep.longrepr)       # JSON turned the skip tuple into a list
            hook.pytest_runtest_logreport(report=rep)
            if rep.when == 'teardown' or (rep.when == 'setup' and not rep.passed):
                u.done.add(rep.nodeid)
            if rep.when == 'teardown':
                hook.pytest_runtest_logfinish(nodeid=rep.nodeid, location=rep.location)

    def finish(self, u: Unit) -> None:
        if u.proc.poll() is None: self.kill(u, 'timed out after %.0f s' % u.timeout)
        self.tail(u)
        code = u.proc.returncode
        why = f'worker died (signal {-code})' if code is not None and code < 0 else f'worker ended with status {code}'
        if getattr(u, 'killed', None): why = u.killed
        for it in u.items:
            if it.nodeid not in u.done:
                rep = TestReport(it.nodeid, it.location, {}, 'failed', f'{why}; see {self.dir / f"{u.n}.log"}', 'call')
                self.config.hook.pytest_runtest_logreport(report=rep)
        self.record_durations(u)
        self.pool.release(u.req.id)
        if u.vmlock is not None: os.close(u.vmlock)

    def record_durations(self, u):
        f = self.dir / f'{u.n}.jsonl'
        if not f.exists(): return
        total = {}
        for line in f.read_text().splitlines():
            ev = json.loads(line)
            if ev['kind'] == 'report':
                d = ev['data']; total[d['nodeid']] = total.get(d['nodeid'], 0) + d.get('duration', 0)
        durations.record_many({k: v for k, v in total.items() if k in {i.nodeid for i in u.items}})

    def kill(self, u: Unit, why: str = 'run interrupted') -> None:
        u.killed = why
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try: os.killpg(u.proc.pid, sig)
            except ProcessLookupError: break
            try: u.proc.wait(5); break
            except subprocess.TimeoutExpired: continue

    def show_waiting(self, waiting, running):
        """Say once, when it changes, why the next unit waits (R7.3)."""
        if not waiting or running: return
        r = self.pool.waiting_reason(waiting[0].req.id)
        if self.reasons.get('head') != r:
            self.reasons['head'] = r
            tr = self.config.pluginmanager.get_plugin('terminalreporter')
            if tr: tr.write_line(f'testkit: waiting: {r}')


@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session):
    cfg = session.config
    if cfg.getoption('testkit_worker') or cfg.getoption('testkit_inprocess'): return None
    if session.testsfailed and cfg.option.continue_on_collection_errors is False: return None
    if not any(isinstance(i, CaseItem) for i in session.items): return None     # plain pytest: pytest's own loop
    if cfg.option.collectonly: return None
    Controller(session).run()
    return True


# --- worker side ---

class Worker:
    def __init__(self, config):
        self.out = open(config.getoption('testkit_report'), 'a', buffering=1)
        self.config = config
        pid = int(os.environ.get('TESTKIT_CONTROLLER_PID', '0'))
        if pid: threading.Thread(target=self.watch, args=(pid,), daemon=True).start()

    @staticmethod
    def watch(pid):
        while True:
            try: os.kill(pid, 0)
            except ProcessLookupError: os._exit(3)      # the controller died: its claim is gone, so stop now
            time.sleep(1)

    def pytest_collection_modifyitems(self, session, config, items):
        want = [l for l in pathlib.Path(config.getoption('testkit_worker')).read_text().splitlines() if l]
        by = {i.nodeid: i for i in items}
        keep = [by[n] for n in want if n in by]
        config.hook.pytest_deselected(items=[i for i in items if i.nodeid not in set(want)])
        items[:] = keep

    def pytest_runtest_logstart(self, nodeid, location):
        self.out.write(json.dumps({'kind': 'start', 'nodeid': nodeid, 'location': location}) + '\n')

    def pytest_runtest_logreport(self, report):
        data = self.config.hook.pytest_report_to_serializable(config=self.config, report=report)
        self.out.write(json.dumps({'kind': 'report', 'data': data}) + '\n')


def pytest_configure(config):
    if config.getoption('testkit_worker'):
        config.pluginmanager.register(Worker(config), 'testkit-worker')

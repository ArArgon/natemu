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

from testkit import contention, durations, known_bugs, paths
from testkit.collect import CaseItem
from testkit.sched import pool as poolmod
from testkit.sched.policy import GATE, INTERACTIVE, Request

POLL = 0.1
LOCK_GIVE_BACK = 10.0               # s a grant may wait for the old VM lock before it goes back to the queue
NO_RAW_CACHES = {'NATEMU_REF_CACHE': 'off', 'NATEMU_ASM_CACHE': 'off', 'NATEMU_QEMU_CACHE': 'off'}
# A stopped worker's group gets SIGTERM, then SIGKILL this long after (if still there): longer than a campaign worker's
# handler may take to stop the natemu it started (testkit.harness.campaign.STOP_WAIT), which a SIGKILL to the group
# would miss (natemu runs in its own session). KILL_GRACE: the controller stopping a unit; WORKER_GRACE: a worker
# whose controller died stopping itself.
KILL_GRACE = 30.0
WORKER_GRACE = 30.0


class Unit:
    def __init__(self, n, items, req, env, timeout):
        self.n, self.items, self.req, self.env, self.timeout = n, items, req, env, timeout
        self.proc = self.grant = self.started = self.vmlock = None
        self.first_test = self.ended = None
        self.lock_wait = None       # since when a granted start waits for the old runners' VM lock
        self.foreign = {}           # exclusive units: busy processes outside the run (testkit.contention), name -> peak %cpu
        self.sampled = 0.0
        self.offset = 0
        self.done = set()


def run_dir(config) -> pathlib.Path:
    """The run's folder: TESTKIT_RUN_DIR under `natemu-test run`, else one ad-hoc folder per pytest session."""
    if (p := getattr(config, '_testkit_run_dir', None)) is not None: return p
    d = os.environ.get('TESTKIT_RUN_DIR')
    p = pathlib.Path(d) if d else paths.runs_dir() / f'adhoc-{time.strftime("%Y%m%d-%H%M%S")}-{uuid.uuid4().hex[:4]}'
    p.mkdir(parents=True, exist_ok=True)
    try: config._testkit_run_dir = p
    except AttributeError: pass
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
    # An item with no recorded duration is estimated from its suite's recorded items (their mean), so one new case does
    # not make its whole unit unknown, which keeps it out of every drain backfill (rule 3; seen: 9 of 12 cores idle while
    # a window drained, the gate's 220 units all unknown because 45 of its thousands of fault cases had a duration).
    known = {}
    for it in items:
        t = table.get(it.nodeid)
        if t is not None: known.setdefault(it.unit_key, []).append(t)
    for k in order:
        g = groups[k]
        s = g[0].suite
        for i in range(0, len(g), max(1, s.batch)):
            chunk = g[i:i + s.batch]
            exp = [table.get(it.nodeid, it.harness.expected(it.case) if hasattr(it.harness, 'expected') else None) for it in chunk]
            if None in exp and known.get(k):
                mean = sum(known[k]) / len(known[k])
                exp = [mean if e is None else e for e in exp]
            nd = it_needs(chunk[0])
            run = run_dir(config).name
            req = Request(id=f'{run}:{uuid.uuid4().hex}', owner_pid=os.getpid(), cores=_cores(nd.cores, pool.cfg),
                          exclusive=nd.exclusive, perf=nd.perf or nd.dedicated, priority=prio,
                          asked=time.time(), expected=None if None in exp else sum(exp),
                          label=f'{g[0].fam.name}/{s.name}' + (f'/{chunk[0].case}' if s.kind == 'legacy' else '')
                                + (f'[{g[0].variant.id}]' if g[0].variant.params else ''),
                          worktree=paths.repo_root().name, run=run)
            if pool.unsatisfiable(req):
                refused += [(it, 'not-runnable', f'needs {req.cores} cores; this machine has {pool.cfg.p_cores} '
                                                 f'performance and {pool.cfg.e_cores} efficiency cores') for it in chunk]
                continue
            env = dict(g[0].place.env)
            if s.kind != 'legacy': env.update(NO_RAW_CACHES)
            h = chunk[0].harness                            # a case may set its own timeout (runner cases: `timeout = N`)
            own = [h.case_timeout(it.fam, it.suite, it.case) for it in chunk] if hasattr(h, 'case_timeout') else []
            timeout = max([float(x) for x in own if x] or [float(s.params.get('timeout', 3600))])
            units.append(Unit(len(units), chunk, req, env, timeout))
    units.sort(key=lambda u: -(u.req.expected if u.req.expected is not None else 1e9))
    return units, refused


def it_needs(item):
    """A suite's claims, or per case where the harness knows them (wrapped legacy steps)."""
    h = item.harness
    return h.case_needs(item.fam, item.suite, item.case) if hasattr(h, 'case_needs') else item.suite.needs


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
        self.known = known_bugs.Tracker(self.config.rootpath)
        rd = self.dir.parent
        try: t0 = json.loads((rd / 'run.json').read_text())['started']
        except (OSError, ValueError, KeyError): t0 = time.time()
        self.t = {'run': rd.name, 'started': t0, 'units': []}      # testkit.timings
        self.config._testkit_timings = self.t

    def save_timings(self) -> None:
        """Never stops the run: a failed write (a full disk) only loses the times."""
        from testkit import timings
        try:
            self.t['units'] = [self.unit_times(u) for u in getattr(self, 'units', [])]
            timings.write(self.dir.parent, self.t)
        except Exception as e:
            print(f'testkit: could not record times: {e}', file=sys.stderr)

    @staticmethod
    def unit_times(u) -> dict:
        it = u.items[0]
        label = u.req.label if len(u.items) > 1 else f'{it.fam.name}/{it.suite.name}/{it.case}'
        return {'label': label, 'family': it.fam.name, 'suite': it.suite.name, 'items': len(u.items),
                'cores': u.req.cores, 'core_class': u.grant.core_class if u.grant else '', 'asked': u.req.asked,
                'granted': u.started, 'first_test': u.first_test, 'ended': u.ended,
                **({'foreign_load': contention.summary(u.foreign)} if u.foreign else {})}

    def prepare_only(self) -> None:
        """`natemu-test prepare` (spec R14.4): build and fetch what the selected items need, under the same shared claim and
        rules as a run, and run nothing. An item whose requirements failed is reported with its category; the others
        are not reported at all."""
        from testkit import prepare
        live = [i for i in self.session.items if isinstance(i, CaseItem) and not i.preset]
        self.t['collected'] = time.time()
        self.t['prepare'] = rec = {'start': time.time()}
        try: failed = prepare.prepare(live, self.pool, self.config, rec)
        finally: rec['end'] = time.time()
        self.save_timings()
        for it in live:
            if it.nodeid in failed:
                report_without_running(it, *failed[it.nodeid])
                self.session.testsfailed += 1
        tr = self.config.pluginmanager.get_plugin('terminalreporter')
        if tr:
            n = len({r for it in live for r in (it.harness.requirements(it.fam, it.suite, it.variant)
                                               if hasattr(it.harness, 'requirements') else [])})
            tr.write_line(f'testkit: prepared {n} requirement(s) for {len(live)} item(s); '
                          f'{len({f[1] for f in failed.values()})} failed' if failed else
                          f'testkit: prepared {n} requirement(s) for {len(live)} item(s); nothing run')

    def run(self) -> None:
        items = self.session.items
        self.t['collected'] = time.time()
        plain = [i for i in items if not isinstance(i, CaseItem) or i.preset]
        for i in plain:
            i.ihook.pytest_runtest_protocol(item=i, nextitem=None)
        if plain: self.t['plain'] = [self.t['collected'], time.time()]
        live = [i for i in items if isinstance(i, CaseItem) and not i.preset]
        from testkit import prepare
        self.t['prepare'] = rec = {'start': time.time()}
        try: failed = prepare.prepare(live, self.pool, self.config, rec)
        finally: rec['end'] = time.time()
        for it in live:
            if it.nodeid in failed: report_without_running(it, *failed[it.nodeid])
        live = [i for i in live if i.nodeid not in failed]
        units, refused = build_units(live, self.config, self.pool)
        self.units = units
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
                    self.watch(u)
                    if u.proc.poll() is not None or time.time() - u.started > u.timeout:
                        self.finish(u); running.remove(u)
                self.show_waiting(waiting, running)
                time.sleep(POLL)
        finally:
            self.kill(*running)
            for u in waiting + running: self.pool.release(u.req.id)
            self.save_timings()

    def start(self, u: Unit, grant) -> bool:
        lock = poolmod.vm_lock_file()
        if lock is not None:
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if grant.core_class == 'X' else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)                        # an old runner (outside the scheduler) holds it
                u.lock_wait = u.lock_wait or time.time()
                if grant.core_class == 'X' and time.time() - u.lock_wait >= LOCK_GIVE_BACK:
                    # Do not hold the machine empty for it: give the grant back unused (not charged to the run) and
                    # queue again at the same place (the request keeps its ask time); while the old lock is held the
                    # pool opens no window and closes an open one, so shared work runs meanwhile.
                    self.pool.release(u.req.id, account=False); self.pool.submit(u.req); u.lock_wait = None
                return False
            u.vmlock = fd
            u.lock_wait = None
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
        env = dict(os.environ, **u.env, TESTKIT_CONTROLLER_PID=str(os.getpid()), TESTKIT_CORE_CLASS=grant.core_class,
                   TESTKIT_VM_GROUPS=str(self.dir / f'{u.n}.vms'))
        log = open(self.dir / f'{u.n}.log', 'wb')
        try:
            u.proc = subprocess.Popen(cmd, cwd=self.config.rootpath, env=env, stdout=log, stderr=subprocess.STDOUT,
                                      start_new_session=True)
        except OSError as e:        # from None: Popen's frames in the traceback would show the environment
            raise RuntimeError(f'could not start a worker for {u.req.label}: {e}') from None
        u.started = time.time()
        return True

    def watch(self, u: Unit) -> None:
        """An exclusive unit: sample foreign load every 5 s (testkit.contention)."""
        if not u.grant or u.grant.core_class != 'X' or time.time() - u.sampled < 5: return
        u.sampled = time.time()
        try:
            for _, cpu, name in contention.sample():
                u.foreign[name] = max(cpu, u.foreign.get(name, 0.0))
        except OSError:
            pass

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
            st = ev['data'].get('start') or 0
            if st and (u.first_test is None or st < u.first_test): u.first_test = st
            if isinstance(rep.longrepr, list): rep.longrepr = tuple(rep.longrepr)       # JSON turned the skip tuple into a list
            if rep.failed and u.foreign:
                note = f'machine not idle during this exclusive run: {contention.summary(u.foreign)}'
                rep.user_properties.append(('testkit-contended', note))
                if isinstance(rep.longrepr, str): rep.longrepr += f'\n[{note}]'
            rep = self.known.adjust(rep)
            hook.pytest_runtest_logreport(report=rep)
            if rep.when == 'teardown' or (rep.when == 'setup' and not rep.passed):
                u.done.add(rep.nodeid)
            if rep.when == 'teardown':
                hook.pytest_runtest_logfinish(nodeid=rep.nodeid, location=rep.location)

    def finish(self, u: Unit) -> None:
        if u.proc.poll() is None: self.kill(u, why='timed out after %.0f s' % u.timeout)
        left = reap_vm_groups(self.dir / f'{u.n}.vms')      # VMs a worker killed outright could not stop
        self.tail(u)
        u.ended = time.time()
        code = u.proc.returncode
        why = f'worker died (signal {-code})' if code is not None and code < 0 else f'worker ended with status {code}'
        if getattr(u, 'killed', None): why = u.killed
        if left: why += f'; stopped {len(left)} VM(s) it left running'
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

    def kill(self, *us: Unit, why: str = 'run interrupted') -> None:
        """SIGTERM to each unit's worker group, then SIGKILL to the groups whose worker is still there after
        KILL_GRACE (one wait for all of them)."""
        for u in us:
            u.killed = why
            if u.ended is None: u.ended = time.time()
        live = []
        for u in us:
            try: os.killpg(u.proc.pid, signal.SIGTERM); live.append(u)
            except ProcessLookupError: pass
        end = time.monotonic() + KILL_GRACE
        for u in live:
            try: u.proc.wait(max(0.0, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                try: os.killpg(u.proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                u.proc.wait()

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
    c = Controller(session)
    with interrupt_on_term():
        c.prepare_only() if cfg.getoption('testkit_prepare_only') else c.run()
    return True


@contextlib.contextmanager
def interrupt_on_term():
    """SIGTERM and SIGHUP (`kill PID`, a closed terminal) raise KeyboardInterrupt, as Ctrl-C does, so the run loop's
    cleanup runs: its workers are stopped (and the VMs they started), its claims released. Without it the controller
    died at once and an orphaned worker's VM held cores and every exclusive window. Once one arrived, later ones are
    ignored, so they do not cut that cleanup short (SIGKILL still ends it)."""
    if threading.current_thread() is not threading.main_thread():
        yield; return
    sigs = (signal.SIGTERM, signal.SIGHUP)
    prev = {s: signal.getsignal(s) for s in sigs}

    def handler(signum, frame):
        for s in sigs: signal.signal(s, signal.SIG_IGN)
        raise KeyboardInterrupt
    for s in sigs: signal.signal(s, handler)
    try:
        yield
    finally:
        for s, h in prev.items(): signal.signal(s, h)


# --- worker side ---

_REAPER = """
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pg, grace = int(sys.argv[1]), float(sys.argv[2])
os.killpg(pg, signal.SIGTERM)
time.sleep(grace)               # the group, this helper a member of it, cannot be reused meanwhile
os.killpg(pg, signal.SIGKILL)   # this helper included
"""


def reap_vm_groups(f: pathlib.Path) -> list[int]:
    """SIGKILL each VM group recorded in `f` (testkit.harness.campaign._record_group: `PID<TAB>PROGRAM` lines) whose
    leader is still alive, still leads its group and still runs PROGRAM (a reused pid is left alone)."""
    try: lines = f.read_text().splitlines()
    except FileNotFoundError: return []
    killed = []
    for line in lines:
        pid, _, prog = line.partition('\t')
        try:
            if not prog or os.getpgid(int(pid)) != int(pid): continue
        except (ValueError, ProcessLookupError, PermissionError): continue
        cmd = subprocess.run(['ps', '-o', 'command=', '-p', pid], capture_output=True, text=True).stdout.strip()
        if cmd != prog and not cmd.startswith(prog + ' '): continue
        try: os.killpg(int(pid), signal.SIGKILL); killed.append(int(pid))
        except (ProcessLookupError, PermissionError): pass
    return killed


class Worker:
    def __init__(self, config):
        self.out = open(config.getoption('testkit_report'), 'a', buffering=1)
        self.config = config
        pid = int(os.environ.get('TESTKIT_CONTROLLER_PID', '0'))
        if pid: threading.Thread(target=self.watch, args=(pid,), daemon=True).start()

    @staticmethod
    def watch(pid):
        """The controller died: its claim is gone, so this worker stops with every child, as the controller's kill()
        would have stopped it: SIGTERM to the group (the campaign harness's handler stops the natemu it started, in its
        own session; a runner script gets it too), then SIGKILL to whatever is left after WORKER_GRACE. A helper in the
        group, ignoring SIGTERM, sends both, so the SIGKILL still comes once this worker has exited."""
        while True:
            try: os.kill(pid, 0)
            except ProcessLookupError: break
            time.sleep(1)
        try:
            subprocess.Popen([sys.executable, '-c', _REAPER, str(os.getpgrp()), str(WORKER_GRACE)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        except OSError:
            os.killpg(0, signal.SIGKILL)

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

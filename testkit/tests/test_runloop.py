import itertools
import json
import os
import subprocess
import sys
import time

import pytest

from testkit import paths


@pytest.fixture
def env(monkeypatch, tmp_path, pytester, family_dir):
    family_dir('sleepfam')
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'cache'))
    monkeypatch.setenv('NATEMU_TEST_CORES', '4,0')
    monkeypatch.setenv('NATEMU_TEST_WINDOW', '600,0')
    monkeypatch.setenv('TESTKIT_RUN_DIR', str(tmp_path / 'run'))
    monkeypatch.setenv('SLEEP_LOG', str(tmp_path / 'sleep.log'))
    return tmp_path


def test_reports_replayed(pytester, env):
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '-k', 'shared', f'--junitxml={env / "j.xml"}', 'tests')
    assert '6 passed' in r.stdout.str(), r.stdout.str()
    assert (env / 'j.xml').read_text().count('<testcase') == 6
    assert len((env / 'run' / 'units').glob('*.jsonl').__iter__().__next__().read_text().splitlines()) > 0


def test_worker_killed_items_error(pytester, env):
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '-k', 'die or shared', 'tests')
    out = r.stdout.str()
    assert r.ret == 1 and '6 passed' in out and '2 failed' in out, out
    assert 'worker died (signal 9)' in out


def test_unpreparable_resource(pytester, env):
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '-k', 'needsbad or shared', 'tests')
    out = r.stdout.str()
    assert '6 passed' in out and '1 unpreparable' in out and 'connection refused' in out, out
    assert r.ret == 1, out          # review: a run where something could not be prepared exited 0 without --gate


def test_two_runs_share_machine(pytester, env):
    procs = []
    for tag in ('a', 'b'):
        e = dict(os.environ, RUN_TAG=tag, TESTKIT_RUN_DIR=str(env / f'run-{tag}'))
        procs.append(subprocess.Popen([sys.executable, '-m', 'pytest', '-p', 'testkit.plugin', '-p', 'no:cacheprovider',
                                       '-q', '-k', 'shared or excl', 'tests'], cwd=pytester.path, env=e,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
    outs = [p.communicate(60)[0].decode() for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    iv = [json.loads(l) for l in (env / 'sleep.log').read_text().splitlines()]
    assert len(iv) == 16 and {x[4] for x in iv} == {'a', 'b'}
    for a, b in itertools.combinations(iv, 2):
        if a[0] < b[1] and b[0] < a[1]: assert not a[2] and not b[2], (a, b)
    for x in iv:
        mid = (x[0] + x[1]) / 2
        assert sum(y[3] for y in iv if y[0] <= mid < y[1]) <= 4


def test_submit_wait_status(env, monkeypatch, tmp_path):
    monkeypatch.delenv('TESTKIT_RUN_DIR')
    fam = paths.repo_root() / 'testkit' / 'tests' / 'fixtures' / 'sleepfam' / 'family.toml'
    e = dict(os.environ, PYTHONPATH=str(paths.repo_root()))
    out = subprocess.run([sys.executable, '-m', 'testkit', 'submit', '-p', 'no:cacheprovider', '-k', 'shared', str(fam)],
                         capture_output=True, text=True, env=e, cwd=paths.repo_root())
    rid = out.stdout.strip()
    assert out.returncode == 0 and len(rid) == 20, out
    st = subprocess.run([sys.executable, '-m', 'testkit', 'status'], capture_output=True, text=True, env=e)
    assert 'machine:' in st.stdout
    w = subprocess.run([sys.executable, '-m', 'testkit', 'wait', rid], capture_output=True, text=True, env=e, timeout=60)
    assert w.returncode == 0 and 'exit status 0' in w.stdout, w.stdout + w.stderr


def test_wait_returns_a_runs_unpreparable_status(env, monkeypatch):
    """Review: `wait RUN` returned 0 for a run whose image could not be built (reported as a skip), while `prepare`
    returned 1."""
    monkeypatch.delenv('TESTKIT_RUN_DIR')
    fam = paths.repo_root() / 'testkit' / 'tests' / 'fixtures' / 'sleepfam' / 'family.toml'
    e = dict(os.environ, PYTHONPATH=str(paths.repo_root()))
    out = subprocess.run([sys.executable, '-m', 'testkit', 'submit', '-p', 'no:cacheprovider', '-k', 'needsbad', str(fam)],
                         capture_output=True, text=True, env=e, cwd=paths.repo_root())
    rid = out.stdout.strip()
    w = subprocess.run([sys.executable, '-m', 'testkit', 'wait', rid], capture_output=True, text=True, env=e, timeout=60)
    assert w.returncode == 1 and 'exit status 1' in w.stdout, w.stdout + w.stderr


def test_prepare_only_runs_nothing(pytester, env):
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '--testkit-prepare-only', '-k', 'shared', 'tests')
    assert r.ret == 0 and 'nothing run' in r.stdout.str(), r.stdout.str()
    assert not (env / 'sleep.log').exists()                      # no test body ran
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '--testkit-prepare-only', '-k', 'needsbad', 'tests')
    out = r.stdout.str()
    assert r.ret == 1 and '1 unpreparable' in out and 'connection refused' in out, out


def test_a_unit_with_unrecorded_items_is_estimated_from_its_suite(monkeypatch, tmp_path):
    """Units must have an expected length to backfill a draining machine: an item with no recorded duration takes the
    mean of its suite's recorded items (9 of 12 cores sat idle while a window drained, every gate unit unknown)."""
    from types import SimpleNamespace as NS
    from testkit import runloop
    from testkit.sched.policy import Config
    suite = NS(name='s', batch=2, kind='runner', params={})
    fam, var = NS(name='f'), NS(id='', params={})
    items = [NS(nodeid=f'n{i}', unit_key=('f', 's', ''), suite=suite, fam=fam, variant=var, case=f'c{i}',
                harness=object(), place=NS(env={})) for i in range(4)]
    other = NS(nodeid='m0', unit_key=('f', 't', ''), suite=NS(name='t', batch=1, kind='runner', params={}), fam=fam,
               variant=var, case='m0', harness=object(), place=NS(env={}))
    monkeypatch.setattr(runloop.durations, 'load', lambda: {'n0': 10.0, 'n1': 20.0})
    monkeypatch.setattr(runloop, 'run_dir', lambda c: tmp_path)
    monkeypatch.setattr(runloop, 'it_needs', lambda it: NS(cores=1, exclusive=False, perf=False, dedicated=False))
    pool = NS(cfg=Config(p_cores=8, e_cores=4), unsatisfiable=lambda r: False)
    config = NS(getoption=lambda k: None)
    units, _ = runloop.build_units(items + [other], config, pool)
    got = sorted(u.req.expected for u in units if u.req.expected is not None)
    assert got == [30.0, 30.0]                          # (10 + 20) and (15 + 15): the unrecorded items take the mean
    assert [u.req.expected for u in units if u.items[0].suite.name == "t"] == [None]   # nothing recorded: still unknown


def test_units_that_never_ran_here_go_first(tmp_path, monkeypatch):
    """A new case's length is only its suite's mean: started late, a long one ends the run (seen: a 9-minute case
    estimated at a minute started 22 minutes into a gate)."""
    from types import SimpleNamespace as NS
    from testkit import runloop
    from testkit.sched.policy import Config
    suite = NS(name='s', batch=1, kind='runner', params={})
    fam, var = NS(name='f'), NS(id='', params={})
    items = [NS(nodeid=f'n{i}', unit_key=('f', 's', ''), suite=suite, fam=fam, variant=var, case=f'c{i}',
                harness=object(), place=NS(env={})) for i in range(4)]
    monkeypatch.setattr(runloop.durations, 'load', lambda: {'n0': 300.0, 'n1': 10.0, 'n3': 200.0})    # n2: never ran
    monkeypatch.setattr(runloop, 'run_dir', lambda c: tmp_path)
    monkeypatch.setattr(runloop, 'it_needs', lambda it: NS(cores=1, exclusive=False, perf=False, dedicated=False))
    pool = NS(cfg=Config(p_cores=8, e_cores=4), unsatisfiable=lambda r: False)
    units, _ = runloop.build_units(items, NS(getoption=lambda k: None), pool)
    assert [u.items[0].nodeid for u in units] == ['n2', 'n0', 'n3', 'n1']
    assert [u.req.asked for u in units] == sorted(u.req.asked for u in units)        # granted in that order


def _alive(pid):
    try: os.kill(pid, 0); return True
    except ProcessLookupError: return False


def _gone_within(pid, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if not _alive(pid): return True
        time.sleep(0.2)
    return False


def test_sigterm_to_the_controller_stops_a_campaign_vm(pytester, env):
    """Review: `kill PID` of the controller killed it outright (no handler, so its cleanup never ran); its worker then
    SIGKILLed its own group, which natemu (its own session) is not in, and the VM ran on, holding cores and every
    exclusive window. Now the controller turns SIGTERM into KeyboardInterrupt, stops its units, and the worker's
    campaign handler stops the VM."""
    pidf = env / 'hold.pid'
    e = dict(os.environ, HOLD_PID=str(pidf))
    c = subprocess.Popen([sys.executable, '-m', 'pytest', '-p', 'testkit.plugin', '-p', 'no:cacheprovider', '-q', '-k', 'hold',
                          'tests'], cwd=pytester.path, env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    end = time.time() + 30
    while not pidf.exists() and time.time() < end: time.sleep(0.1)
    assert pidf.exists(), c.communicate(timeout=60)[0]
    vm = int(pidf.read_text())
    try:
        c.terminate()
        out = c.communicate(timeout=60)[0].decode()
        assert _gone_within(vm, 5), out
    finally:
        if _alive(vm): os.kill(vm, 9)


def test_a_sigkilled_workers_vm_is_stopped_by_the_controller(pytester, env):
    """Review: a worker killed outright (SIGKILL: no handler runs) left the natemu it started running in its own session,
    holding cores with no cap. The worker records each VM's group in its unit folder; the controller ending the unit
    SIGKILLs a recorded group that is still there."""
    pidf = env / 'hold.pid'
    e = dict(os.environ, HOLD_PID=str(pidf))
    c = subprocess.Popen([sys.executable, '-m', 'pytest', '-p', 'testkit.plugin', '-p', 'no:cacheprovider', '-q', '-k', 'hold',
                          'tests'], cwd=pytester.path, env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    end = time.time() + 30
    while not pidf.exists() and time.time() < end: time.sleep(0.1)
    assert pidf.exists(), c.communicate(timeout=60)[0]
    vm = int(pidf.read_text())
    worker = int(subprocess.run(['ps', '-o', 'ppid=', '-p', str(vm)], capture_output=True, text=True).stdout)
    try:
        os.kill(worker, 9)
        out = c.communicate(timeout=60)[0].decode()
        assert 'worker died (signal 9)' in out, out
        assert _gone_within(vm, 5), out
    finally:
        if _alive(vm): os.kill(vm, 9)


def test_reaping_kills_only_recorded_groups_still_running_their_program(tmp_path):
    from testkit import runloop
    vm = subprocess.Popen(['sleep', '60'], start_new_session=True)
    other = subprocess.Popen(['sleep', '61'], start_new_session=True)      # recorded with another program: a reused pid
    dead = subprocess.Popen(['true'], start_new_session=True); dead.wait()
    f = tmp_path / '0.vms'
    f.write_text(f'{vm.pid}\tsleep\n{other.pid}\t/opt/natemu/stub\n{dead.pid}\tsleep\n')
    try:
        assert runloop.reap_vm_groups(f) == [vm.pid]
        vm.wait(5)
        assert other.poll() is None
        assert runloop.reap_vm_groups(tmp_path / 'none.vms') == []
    finally:
        for p in (vm, other):
            if p.poll() is None: p.kill()


def test_a_vm_the_harness_stopped_is_not_reaped_when_its_pid_is_reused(tmp_path):
    """Review: a pid recorded for a VM the harness stopped itself can come back as another unit's VM of the same
    program; the harness's `PID<TAB>` line after the stop keeps the reaper away from it."""
    from testkit import runloop
    reused = subprocess.Popen(['sleep', '60'], start_new_session=True)
    f = tmp_path / '0.vms'
    f.write_text(f'{reused.pid}\tsleep\n{reused.pid}\t\n')
    try:
        assert runloop.reap_vm_groups(f) == [] and reused.poll() is None
        f.write_text(f.read_text() + f'{reused.pid}\tsleep\n')     # booted again under that pid: ours once more
        assert runloop.reap_vm_groups(f) == [reused.pid]
    finally:
        if reused.poll() is None: reused.kill()


def test_an_orphaned_worker_gives_its_handlers_time_then_kills_its_group(tmp_path):
    """Review: a worker whose controller died SIGKILLed its group at once, so its campaign handler never stopped the
    natemu it started (its own session). Now the group gets SIGTERM first and SIGKILL only after WORKER_GRACE."""
    import textwrap
    dead = subprocess.Popen(['true']); dead.wait()
    script = tmp_path / 'worker.py'
    script.write_text(textwrap.dedent(f'''
        import subprocess, sys, threading, time
        sys.path.insert(0, {str(paths.repo_root())!r})
        from testkit import runloop
        from testkit.harness import campaign
        runloop.WORKER_GRACE = 2
        vm = subprocess.Popen(['sleep', '60'], start_new_session=True)
        campaign._LIVE.add(vm); campaign._on_term()
        stubborn = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, '
                                     'signal.SIG_IGN); print(flush=True); time.sleep(60)'], stdout=subprocess.PIPE)
        assert stubborn.stdout.readline() == b'\\n'  # it ignores SIGTERM before the controller is found dead (not a fixed sleep:
        print(vm.pid, stubborn.pid, flush=True)    # a slow start would have let SIGTERM kill it, and the test pass untested)
        threading.Thread(target=runloop.Worker.watch, args=({dead.pid},), daemon=True).start()
        time.sleep(60)
    '''))
    w = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True, start_new_session=True)
    vm, stubborn = map(int, w.stdout.readline().split())
    try:
        assert _gone_within(vm, 20), 'the VM outlived its orphaned worker'
        w.wait(20)
        assert _gone_within(stubborn, 20), 'a child ignoring SIGTERM outlived the grace'
    finally:
        for p in (vm, stubborn, w.pid):
            if _alive(p): os.kill(p, 9)


def test_kill_waits_longer_than_a_campaign_stop(tmp_path):
    """Review: kill() sent SIGKILL 5 s after SIGTERM, while a campaign worker's handler may take campaign.STOP_WAIT
    (natemu's clean stop, then its SIGKILL) to stop its VM."""
    from testkit import runloop
    from testkit.harness import campaign
    assert runloop.KILL_GRACE > campaign.STOP_WAIT and runloop.WORKER_GRACE > campaign.STOP_WAIT
    mark = tmp_path / 'stopped'
    slow = (f'import pathlib, signal, sys, time\n'
            f'def h(*_): time.sleep(6); pathlib.Path({str(mark)!r}).write_text("x"); sys.exit(0)\n'
            f'signal.signal(signal.SIGTERM, h)\nprint(flush=True)\ntime.sleep(60)\n')
    p = subprocess.Popen([sys.executable, '-c', slow], start_new_session=True, stdout=subprocess.PIPE)
    p.stdout.readline()

    class U: proc, ended = p, None
    runloop.Controller.kill(None, U())
    assert mark.exists()


def test_a_unit_on_an_efficiency_core_records_performance_core_durations(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from testkit import durations, runloop
    from testkit.sched.policy import Config
    got = []
    monkeypatch.setattr(durations, 'record_many', lambda d: got.append(d))
    (tmp_path / '0.jsonl').write_text('\n'.join(json.dumps({'kind': 'report', 'data': {'nodeid': n, 'duration': t}})
                                                for n, t in (('a', 30.0), ('a', 6.0), ('b', 9.0), ('other', 1.0))))
    ctl = NS(dir=tmp_path, pool=NS(cfg=Config(p_cores=8, e_cores=4, e_slowdown=3.0)))
    for cls, want in (('E', {'a': 12.0, 'b': 3.0}), ('P', {'a': 36.0, 'b': 9.0})):
        u = NS(n=0, items=[NS(nodeid='a'), NS(nodeid='b')], grant=NS(core_class=cls))
        runloop.Controller.record_durations(ctl, u)
        assert got.pop() == want

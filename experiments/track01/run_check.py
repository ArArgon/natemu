#!/usr/bin/env python3
"""Retain and supervise a read-only Track 01 oracle; never launches/retries a VM."""
import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

WALL_SECONDS = 1800
RSS_BYTES = 4 * 1024 ** 3
Q31_RSS_BYTES = 6 * 1024 ** 3
Q32_RSS_BYTES = 8 * 1024 ** 3
Q33_RSS_BYTES = 10 * 1024 ** 3
Q34_RSS_BYTES = 12 * 1024 ** 3
PERIOD_SECONDS = 1
SOURCES = ('check_q30.py', 'x86_walk.py', 'check_foundation.py')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def inventory(directory):
    return {str(p.relative_to(directory)): digest(p)
            for p in sorted(directory.rglob('*')) if p.is_file() and p.name != 'SHA256SUMS'}


def save(directory, record):
    temporary = directory / 'supervision.tmp'
    temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
    temporary.replace(directory / 'supervision.json')


def allocate(root, repo, sealed, name):
    if not root or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', name):
        raise ValueError('NATEMU_RAW_ROOT and a safe fresh name are required')
    root = Path(root).expanduser().resolve()
    repo = Path(repo).resolve()
    sealed = tuple(Path(p).resolve() for p in sealed)
    target = root / name
    if root == repo or repo in root.parents or any(target == p or p in target.parents for p in sealed):
        raise ValueError('checker output must be outside repository and sealed VM run')
    root.mkdir(parents=True, exist_ok=True)
    target.mkdir()  # Existing directories and symlinks are never reused.
    return target


def child_limits():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024,) * 2)


def group_rss(pgid, timeout=5):
    # ps(1): rss is KiB. Sum only this new session's group; shared pages can
    # count twice. No process arguments or environment are queried/retained.
    output = subprocess.check_output(
        ['/bin/ps', '-A', '-o', 'pid=,pgid=,rss='], text=True, timeout=timeout)
    members = []
    for line in output.splitlines():
        pid, group, kib = map(int, line.split())
        if group == pgid:
            members.append((pid, kib * 1024))
    return sum(rss for _, rss in members), members


class _DarwinSiginfo(ctypes.Structure):
    # Local Darwin sys/signal.h: six 32-bit fields, then ten pointer/long slots.
    _fields_ = [('si_signo', ctypes.c_int), ('si_errno', ctypes.c_int),
                ('si_code', ctypes.c_int), ('si_pid', ctypes.c_int),
                ('si_uid', ctypes.c_uint), ('si_status', ctypes.c_int),
                ('remaining', ctypes.c_uint64 * 10)]


def peek_child(child):
    """Observe only our live Popen lease; never reap before group signalling."""
    if child.returncode is not None:
        raise ChildProcessError(errno.ECHILD, 'supervisor child lease already reaped')
    while True:
        try:
            if hasattr(os, 'waitid'):
                info = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            elif sys.platform == 'darwin':
                if ctypes.sizeof(_DarwinSiginfo) != 104:
                    raise RuntimeError('unsupported Darwin siginfo layout')
                libc = ctypes.CDLL(None, use_errno=True)
                waitid = libc.waitid
                waitid.argtypes = (ctypes.c_uint, ctypes.c_uint,
                                   ctypes.POINTER(_DarwinSiginfo), ctypes.c_int)
                waitid.restype = ctypes.c_int
                info = _DarwinSiginfo()
                if waitid(1, child.pid, ctypes.byref(info), 0x4 | 0x1 | 0x20) != 0:
                    error = ctypes.get_errno()
                    raise OSError(error, os.strerror(error))
            else:
                raise RuntimeError('non-reaping child observation unavailable')
            break
        except InterruptedError:
            continue
    if info is None or info.si_pid == 0:
        return None
    if info.si_pid != child.pid or info.si_code not in (1, 2, 3):
        raise RuntimeError('unexpected non-reaping child identity/status')
    return info.si_status if info.si_code == 1 else -info.si_status


def exited_group_members(child, timeout):
    """Darwin can return EPERM for an all-zombie group. Prove no live member.

    Keep the leader waitable across the entire observation; RSS zero alone says
    nothing about liveness and is deliberately not used here.
    """
    if peek_child(child) is None:
        raise RuntimeError('signalling failed while owned leader remains live')
    output = subprocess.check_output(
        ['/bin/ps', '-A', '-o', 'pid=,pgid=,stat='], text=True, timeout=timeout)
    members = []
    for line in output.splitlines():
        pid, group, state = line.split()
        if int(group) == child.pid:
            members.append(dict(pid=int(pid), pgid=int(group), state=state))
    if any(not member['state'].startswith('Z') for member in members):
        raise RuntimeError('signalling failed with a live owned-group member')
    if peek_child(child) is None:
        raise RuntimeError('owned leader changed during group observation')
    return members


def supervise(argv, directory, wall=WALL_SECONDS, rss_limit=RSS_BYTES, period=PERIOD_SECONDS,
              gate_file=None):
    record = {'command': argv, 'cwd': str(directory), 'wall_seconds': wall,
              'rss_bytes': rss_limit, 'sample_period_seconds': period,
              'ps_timeout_seconds': 5, 'peak_observed_group_rss_bytes': 0,
              'process_reaped': False,
              'rss_samples': 0, 'status': 'running', 'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    start = time.monotonic()
    child = None
    gate_deadline = None
    with (directory / 'stdout').open('xb') as stdout, (directory / 'stderr').open('xb') as stderr, \
            (directory / 'rss.jsonl').open('x') as samples:
        try:
            child = subprocess.Popen(argv, cwd=directory, stdout=stdout, stderr=stderr,
                                     start_new_session=True, preexec_fn=child_limits)
            record['pid'] = child.pid
            while True:
                elapsed = time.monotonic() - start
                if gate_file is not None and gate_deadline is None and Path(gate_file).exists():
                    gate=json.loads(Path(gate_file).read_text())
                    remaining=gate['deadline_unix_ms']/1000-time.time()
                    if not (type(gate['host_pid']) is int and gate['host_pid']>0 and remaining<=10):
                        raise ValueError('invalid containment watchdog gate')
                    gate_deadline=time.monotonic()+max(0,remaining)
                    record['gate']=gate
                if gate_deadline is not None and time.monotonic()>=gate_deadline:
                    record['status']='containment_outer_limit'
                    break
                if elapsed >= wall:
                    record['status'] = 'wall_limit'
                    break
                if peek_child(child) is not None:
                    break
                sample_start = time.monotonic()
                sample_bound=min(.25 if gate_file is not None else 5,wall-elapsed)
                if gate_deadline is not None:sample_bound=min(sample_bound,max(.001,gate_deadline-time.monotonic()))
                rss, members = group_rss(child.pid, sample_bound)
                if not members and peek_child(child) is None:
                    raise RuntimeError('live oracle missing from process-group sample')
                record['peak_observed_group_rss_bytes'] = max(record['peak_observed_group_rss_bytes'], rss)
                record['rss_samples'] += 1
                samples.write(json.dumps({'elapsed_seconds': time.monotonic() - start,
                                          'rss_bytes': rss, 'members': members}) + '\n')
                samples.flush()
                if rss > rss_limit:
                    record['status'] = 'rss_limit'
                    break
                time.sleep(max(0, min(sample_start + period, start + wall,
                    gate_deadline if gate_deadline is not None else start+wall) - time.monotonic()))
        except BaseException as error:
            record['status'] = ('containment_outer_limit'
                if isinstance(error,subprocess.TimeoutExpired) and gate_deadline is not None and time.monotonic()>=gate_deadline
                else 'wall_limit' if time.monotonic() - start >= wall else 'supervisor_error')
            record['error'] = type(error).__name__ + ': ' + str(error)
        finally:
            if child is not None:
                lease = False
                try:
                    # Recheck ownership even on monitor errors. ECHILD cannot
                    # authorize a signal to a potentially reused numeric PGID.
                    peek_child(child)
                    lease = True
                except BaseException as error:
                    record['lease_error'] = type(error).__name__ + ': ' + str(error)
                    if record['status'] == 'running':record['status'] = 'supervisor_error'
                if lease:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                        record['group_signalled'] = True
                    except ProcessLookupError:
                        record['group_absent'] = True
                    except OSError as error:
                        proved_quiescent = False
                        if sys.platform == 'darwin' and error.errno == errno.EPERM:
                            try:
                                record['quiescent_group_members'] = exited_group_members(child, .25 if gate_file else 5)
                                record['group_quiescent'] = True
                                proved_quiescent = True
                            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as observation:
                                record['group_observation_error'] = str(observation)
                        if not proved_quiescent:
                            record['signal_error'] = str(error)
                            if record['status'] == 'running':record['status'] = 'supervisor_error'
                    try:
                        record['returncode'] = child.wait(timeout=5)
                        record['process_reaped'] = True
                    except (OSError, subprocess.TimeoutExpired) as error:
                        record['reap_error'] = str(error)
                        if record['status'] == 'running':record['status'] = 'supervisor_error'
            record['elapsed_seconds'] = time.monotonic() - start
            record['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    if record['status'] == 'running':
        try:
            report = json.loads((directory / 'stdout').read_text())
            record['oracle_status'] = report['status']
            record['status'] = ('passed' if record['returncode'] == 0 and report['status'] == 'PASS'
                                else 'incomplete' if record['returncode'] == 2 and report['status'] == 'INCOMPLETE'
                                else 'oracle_failed')
        except (ValueError, KeyError, TypeError) as error:
            record['status'] = 'oracle_failed'
            record['error'] = 'invalid oracle report: ' + str(error)
    return record


def self_check():
    with tempfile.TemporaryDirectory(prefix='natemu-supervisor-') as temporary:
        root = Path(temporary)
        programs = [('pass', "import json; print(json.dumps({'status':'PASS'}))", 'passed', 5, RSS_BYTES),
                    ('nonzero', "print('{\"status\":\"PASS\"}'); raise SystemExit(1)", 'oracle_failed', 5, RSS_BYTES),
                    ('incomplete', "print('{\"status\":\"INCOMPLETE\"}'); raise SystemExit(2)", 'incomplete', 5, RSS_BYTES),
                    ('wall', 'import time; time.sleep(10)', 'wall_limit', .15, RSS_BYTES),
                    ('rss', 'import time; time.sleep(10)', 'rss_limit', 5, 1)]
        for name, code, wanted, wall, rss in programs:
            out = root / name; out.mkdir()
            result = supervise([sys.executable, '-B', '-c', code], out, wall, rss, .05)
            assert result['status'] == wanted, result
            if name in ('wall', 'rss'):
                assert result['returncode'] < 0, result
        out = root / 'monitor'; out.mkdir()
        with patch(__name__ + '.group_rss', side_effect=OSError('synthetic monitor failure')):
            result = supervise([sys.executable, '-B', '-c', 'import time; time.sleep(10)'], out)
        assert result['status'] == 'supervisor_error' and result['returncode'] < 0, result
        out = root / 'descendant'; out.mkdir()
        code = ("import subprocess,sys,time,pathlib; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)']); "
                "pathlib.Path('descendant.pid').write_text(str(p.pid)); "
                "print('{\"status\":\"PASS\"}'); time.sleep(.25)")
        result = supervise([sys.executable, '-B', '-c', code], out, 5, RSS_BYTES, .05)
        assert result['status'] == 'passed', result
        assert any(len(json.loads(line)['members']) >= 2 for line in (out / 'rss.jsonl').read_text().splitlines())
        pid = int((out / 'descendant.pid').read_text())
        state = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True, timeout=5)
        assert not state.stdout.strip() or state.stdout.strip().startswith('Z'), state.stdout
        repo = root / 'repo'; repo.mkdir()
        run = root / 'sealed'; run.mkdir()
        oracle = root / 'oracle'; oracle.mkdir()
        fresh = allocate(str(root), repo, (run, oracle), 'fresh')
        for output_root, name in ((str(root), 'fresh'), (str(repo), 'bad'), (str(run), 'bad'), (str(oracle), 'bad'), (str(root), '../bad')):
            try:
                allocate(output_root, repo, (run, oracle), name)
            except (ValueError, FileExistsError):
                pass
            else:
                raise AssertionError('unsafe/reused output accepted')

    print('PASS supervisor self-check: PASS/exit, INCOMPLETE, wall/RSS/monitor stops, output guards; no VM')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-check', action='store_true')
    stage = parser.add_mutually_exclusive_group()
    stage.add_argument('--q31', action='store_true', help='Q31 continuation oracle and its explicit 6 GiB RSS ceiling')
    stage.add_argument('--q32', action='store_true', help='Q32 continuation oracle and its explicit 8 GiB RSS ceiling')
    stage.add_argument('--q33', action='store_true', help='Q33 continuation oracle and its explicit 10 GiB RSS ceiling')
    stage.add_argument('--q34', action='store_true', help='Q34 continuation oracle and its explicit 12 GiB RSS ceiling')
    parser.add_argument('--run', type=Path)
    parser.add_argument('--oracle', type=Path)
    parser.add_argument('--name')
    args = parser.parse_args()
    if args.self_check:
        self_check(); return 0
    if args.run is None or args.oracle is None or args.name is None:
        parser.error('--run, --oracle and --name are required')
    repo = next((p for p in (Path.cwd(), *Path.cwd().parents)
                 if (p / 'AGENTS.md').is_file() and (p / 'docs/research/README.md').is_file()), None)
    if repo is None:
        parser.error('invoke from repository')
    run, oracle = args.run.resolve(), args.oracle.resolve()
    output = allocate(os.environ.get('NATEMU_RAW_ROOT'), repo.resolve(), (run, oracle), args.name)
    question = 34 if args.q34 else 33 if args.q33 else 32 if args.q32 else 31 if args.q31 else 30
    sources = SOURCES + tuple(f'check_q{q}.py' for q in range(31, question + 1))
    rss_limit = Q34_RSS_BYTES if args.q34 else Q33_RSS_BYTES if args.q33 else Q32_RSS_BYTES if args.q32 else Q31_RSS_BYTES if args.q31 else RSS_BYTES
    record = {'format': f'natemu-q{question}-supervision-v2',
              'vm_run': str(run), 'oracle_origin': str(oracle),
              'status': 'preparing', 'scope': 'read-only oracle; never retry VM'}
    save(output, record)
    try:
        if any(p.is_symlink() for p in oracle.rglob('*')):
            raise ValueError('oracle symlink')
        record['vm_inventory_sha256'] = digest(run / 'SHA256SUMS')
        source = output / 'source'; source.mkdir()
        for name in sources:
            shutil.copyfile(oracle / name, source / name)
        shutil.copyfile(Path(__file__), source / 'run_check.py')
        record['sources'] = inventory(source)
        checker = f'check_q{question}.py'
        result = supervise([sys.executable, '-B', str(source / checker), str(run)], output,
                           rss_limit=rss_limit)
        record.update(result)
        if (digest(run / 'SHA256SUMS') != record['vm_inventory_sha256'] or
                inventory(source) != record['sources']):
            record['status'] = 'input_changed'
    except BaseException as error:
        record['status'] = 'supervisor_error'
        record['error'] = type(error).__name__ + ': ' + str(error)
    finally:
        save(output, record)
        (output / 'SHA256SUMS').write_text(''.join(h + '  ' + n + '\n' for n, h in inventory(output).items()))
    print(json.dumps({'output': str(output), 'status': record['status']}, sort_keys=True))
    return 0 if record['status'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())

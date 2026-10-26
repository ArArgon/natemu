#!/usr/bin/env python3
"""Per-request ownership journal; never discover or remove global containers.

The host reserves a job before spawning its process group. Container intentions
are durable before create; cleanup does not depend on compiler.json surviving.
"""
import argparse
import json
import os
from pathlib import Path
import re
import runpy
import sys
import traceback
import signal
import subprocess
import time
import uuid

SCHEMA = 'natemu-owned-worker-v1'
ENV = 'NATEMU_WORKER_REGISTRY'


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        with temporary.open('x') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_bytes(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(value); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('oversized ownership record')
    return json.loads(raw)


def job(registry):
    value = read_json(Path(registry) / 'job.json')
    if value.get('schema') != SCHEMA or not re.fullmatch('[0-9a-f]{32}', value.get('token', '')):
        raise ValueError('invalid owned-worker registry')
    return value


def reserve(registry, *, kind="standalone-container"):
    """Standalone probes use the same journal without claiming a host worker."""
    if kind not in (None, 'standalone-container'):
        raise ValueError('unknown ownership registry kind')
    registry = Path(registry)
    registry.mkdir()
    value = dict(schema=SCHEMA, token=uuid.uuid4().hex, host_pid=os.getpid())
    if kind is not None:value['kind'] = kind
    atomic_json(registry / 'job.json', value)
    return value


def ensure_open(registry):
    if (Path(registry) / 'cancelled').exists():
        raise RuntimeError('owned compiler job cancelled')


class Container:
    def __init__(self, directory, name):
        if not re.fullmatch('natemu-[a-z0-9-]+', name):
            raise ValueError('invalid owned container name')
        configured = os.environ.get(ENV)
        self.registry = Path(configured).resolve() if configured else Path(directory).resolve() / '.ownership'
        if not configured:
            reserve(self.registry)
        owner = job(self.registry)
        ensure_open(self.registry)
        records = self.registry / 'containers'; records.mkdir(exist_ok=True)
        self.path = records / (name + '.json')
        if self.path.exists():
            raise ValueError('container ownership already registered')
        self.record = dict(schema=SCHEMA, token=owner['token'], name=name, state='registered')
        atomic_json(self.path, self.record)

    @property
    def label(self):
        return 'natemu.job=' + self.record['token']

    def update(self, state, **fields):
        self.record.update(state=state, **fields)
        atomic_json(self.path, self.record)

    def creating(self):
        ensure_open(self.registry)
        self.update('creating')

    def created(self, identity):
        identity = identity.strip()
        if not re.fullmatch('[0-9a-f]{64}', identity):
            raise ValueError('container creation returned no immutable identity')
        self.update('created', container_id=identity)
        if (self.registry / 'hold-container').exists():
            # Publish only after the daemon acknowledged an immutable container.
            atomic_json(self.registry / 'creation-held.json', self.record)
            atomic_bytes(self.registry / 'container-ready.bin',
                         b'NATCTR01' + bytes.fromhex(self.record['token']) +
                         bytes.fromhex(identity) + bytes(8))

    def wait_before_start(self):
        while (self.registry / 'hold-container').exists():
            ensure_open(self.registry)
            time.sleep(.01)
        ensure_open(self.registry)

    def removed(self):
        self.update('removed')


def _inspect(identity, run, timeout, *, name=None):
    result = run(['docker', 'inspect', identity], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Docker clients differ in error capitalization. Only an explicit
        # absence response can acknowledge removal; daemon failures cannot.
        error = result.stderr.casefold()
        if 'no such object:' in error or 'no such container:' in error:
            return None
        raise RuntimeError('container inspection failed: ' + result.stderr.strip())
    records = json.loads(result.stdout)
    if len(records) != 1 or (name is not None and records[0].get('Name') != '/' + name):
        raise ValueError('container inspection identity mismatch')
    if not re.fullmatch('[0-9a-f]{64}', records[0].get('Id', '')):
        raise ValueError('container inspection has no immutable identity')
    if name is None and records[0]['Id'] != identity:
        raise ValueError('container immutable identity changed')
    return records[0]


def cleanup_containers(registry, *, run=subprocess.run, budget=12):
    """Bounded, exact-name+label cleanup. Unacknowledged creation fails closed."""
    registry = Path(registry).resolve(); owner = job(registry)
    deadline = time.monotonic() + budget
    report = dict(schema=SCHEMA, token=owner['token'], complete=True, containers=[])
    for path in sorted((registry / 'containers').glob('*.json')):
        item = dict(record=path.name, complete=False)
        report['containers'].append(item)
        try:
            record = read_json(path); name = record['name']
            if record.get('token') != owner['token'] or path.name != name + '.json' or not re.fullmatch('natemu-[a-z0-9-]+', name):
                raise ValueError('container registry identity mismatch')
            item.update(name=name, state_before=record['state'])
            if record['state'] == 'removed':
                item.update(complete=True, removal_acknowledged=True)
                continue
            def timeout():
                left = deadline - time.monotonic()
                if left <= 0: raise TimeoutError('owned container cleanup deadline')
                return min(left, 3)
            observed = _inspect(name, run, timeout(), name=name)
            item['observed'] = observed is not None
            if observed is not None:
                if observed.get('Config', {}).get('Labels', {}).get('natemu.job') != owner['token']:
                    raise ValueError('refusing to remove container with foreign ownership label')
                identity = observed['Id']
                if record.get('container_id', identity) != identity:
                    raise ValueError('container identity differs from creation acknowledgement')
                item['container_id'] = identity
                result = run(['docker','rm','--force','--volumes',identity], capture_output=True, text=True, timeout=timeout())
                if result.returncode: raise RuntimeError('owned container removal failed: '+result.stderr.strip())
                if _inspect(identity, run, timeout()) is not None:
                    raise RuntimeError('owned container remains after removal')
            elif record['state'] == 'creating':
                raise RuntimeError('unacknowledged container create; absence does not prove daemon request completed')
            item['complete'] = True
            record['state'] = 'removed'; atomic_json(path,record)
        except Exception as error:
            item['error'] = f'{type(error).__name__}: {error}'
            report['complete'] = False
    atomic_json(registry / 'cleanup.json', report)
    return report


def process_start(pid):
    try:
        result=subprocess.run(['/bin/ps','-o','lstart=','-p',str(pid)],capture_output=True,text=True,timeout=2)
    except PermissionError:
        return None  # The owning Rust Child lease still prevents PID reuse.
    if result.returncode not in (0,1):raise RuntimeError('worker identity inspection failed')
    return result.stdout.strip() or None


def launch(registry, argv):
    registry = Path(registry).resolve(); owner = job(registry)
    pid = os.getpid()
    if os.getpgrp() != pid:
        raise ValueError('owned worker must lead its dedicated process group')
    atomic_json(registry/'worker.json', dict(schema=SCHEMA,token=owner['token'],pid=pid,pgid=pid,parent_pid=os.getppid(),started=process_start(pid)))
    atomic_bytes(registry/'worker-ready.bin', b'NATWRK01' + bytes.fromhex(owner['token']) + pid.to_bytes(8, 'little'))
    ensure_open(registry)
    while (registry/'hold-payload').exists():
        ensure_open(registry)
        time.sleep(.01)
    os.environ[ENV] = str(registry)
    # Keep the unreaped group leader alive after completion. The Rust owner
    # kills this group before reaping, so a deferred result cannot reuse a PID.
    if argv[:2] != ['python3', '-B'] or len(argv) < 3:
        raise ValueError('owned worker requires a Python source entry point')
    sys.dont_write_bytecode = True
    sys.argv = argv[2:]
    sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
    status = 0
    try:
        runpy.run_path(sys.argv[0], run_name='__main__')
    except SystemExit as error:
        status = error.code if isinstance(error.code, int) else (0 if error.code is None else 1)
    except BaseException:
        traceback.print_exc(); status = 1
    sys.stdout.flush(); sys.stderr.flush()
    atomic_json(registry/'worker-result.json',dict(schema=SCHEMA,token=owner['token'],exit_code=status,pid=pid))
    temporary = registry/'result.pending'
    with temporary.open('xb') as stream:
        stream.write(((status & 255) << 8).to_bytes(4,'little'));stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,registry/'result.bin')
    while True:
        time.sleep(60)


def cleanup_standalone(registry, *, expected_root, supervision, budget=15):
    """Container-only cleanup for a caller-bound, externally supervised probe.

    The runner must map expected_root to its actual supervised job. This API
    does not claim ownership of, signal, or acknowledge reaping any process.
    """
    registry = Path(registry).resolve(); owner = job(registry)
    if registry != Path(expected_root).resolve() / '.ownership':
        raise ValueError('standalone registry differs from supervised compiler root')
    if owner.get('kind') != 'standalone-container':
        raise ValueError('container-only cleanup requires explicit standalone registry')
    if any((registry / name).exists() for name in ('worker.json', 'worker-ready.bin',
            'worker-result.json', 'worker-exit.json', 'result.bin', 'process.json')):
        raise ValueError('standalone registry contains owned-worker lifecycle records')
    (registry/'cancelled').touch(exist_ok=True)
    report = cleanup_containers(registry, budget=max(0, budget))
    report['scope'] = 'standalone-containers'
    receipt = {key:supervision.get(key) for key in ('pid','status','returncode','process_reaped')}
    outcomes = ('group_signalled','group_absent','group_quiescent')
    errors = ('lease_error','signal_error','group_observation_error','reap_error')
    for key in outcomes + errors + ('quiescent_group_members',):
        if key in supervision:receipt[key] = supervision[key]
    group_complete = any(receipt.get(key) is True for key in outcomes) and not any(key in receipt for key in errors)
    terminal = receipt['status'] in ('passed','incomplete','oracle_failed','wall_limit',
        'rss_limit','containment_outer_limit','supervisor_error')
    reaped = (terminal and type(receipt['pid']) is int and receipt['pid'] > 0
        and type(receipt['returncode']) is int and receipt['process_reaped'] is True and group_complete)
    report['external_supervision'] = receipt
    if not reaped:
        report['process_error'] = 'external supervisor did not acknowledge terminal process reaping and safe group cleanup'
    report['complete'] &= reaped
    atomic_json(registry/'cleanup.json', report)
    return report


def cleanup_job(registry, *, budget=15):
    """External supervisor hook after host joins; journal survives worker death."""
    registry=Path(registry).resolve();owner=job(registry)
    if owner.get('kind') == 'standalone-container':
        raise ValueError('standalone registry requires explicitly bound external supervision')
    (registry/'cancelled').touch(exist_ok=True)
    process=dict(schema=SCHEMA,token=owner['token'],signalled=False,group_gone=False)
    # Only the live Rust Child lease can authorize killpg without a PID reuse
    # race. External fallback observes its durable acknowledgement; it never
    # treats missing registration or coarse ps timestamps as ownership proof.
    prior=registry/'process.json'
    if prior.exists():
        recorded=read_json(prior)
        if recorded.get('token')!=owner['token']:
            raise ValueError('process cleanup ownership mismatch')
        process=recorded
        if not process.get('group_gone',False):
            process['error']='owning host did not acknowledge process group drainage'
    else:
        process['error']='no owning-host process cleanup acknowledgement'
    atomic_json(registry/'supervisor-process.json',process)
    report=cleanup_containers(registry,budget=max(0,budget))
    report['process']=process
    process_complete = process.get('group_gone') is True and (
        process.get('reaped') is True or process.get('spawned') is False)
    if not process_complete and 'error' not in process:
        process['error'] = 'owning host did not acknowledge reaping its worker'
    report['complete'] &= process_complete
    atomic_json(registry/'cleanup.json',report)
    return report


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('worker','cleanup'))
    parser.add_argument('--registry',required=True,type=Path)
    parser.add_argument('--budget',type=float,default=2)
    args,command=parser.parse_known_args()
    if args.operation=='worker':
        if command[:1]==['--']:command=command[1:]
        if not command:parser.error('missing worker command')
        launch(args.registry,command)
    else:
        result=cleanup_containers(args.registry,budget=args.budget)
        print(json.dumps(result))
        raise SystemExit(0 if result['complete'] else 1)

#!/usr/bin/env python3
"""Retain one bounded Docker toolchain setup; never run a guest program."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
BASE = 'sha256:8bebfca538d54dde4c4f891af9e237627ac4293c190381f6e988e3b6955137c6'


def load(path):
    spec = importlib.util.spec_from_file_location('retention', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', required=True)
    args = parser.parse_args()
    helper = REPO / 'experiments/track01/source/run.py'
    retention = load(helper)
    run = retention.allocate(os.environ.get('NATEMU_RAW_ROOT'), REPO, args.name)
    source = run / 'source'
    source.mkdir()
    for path in (HERE / 'setup.py', HERE / 'setup.sh', helper):
        target = source / ('retention.py' if path == helper else path.name)
        shutil.copyfile(path, target)
        if path.read_bytes() != target.read_bytes():
            raise RuntimeError('source changed during snapshot')
    retention = load(source / 'retention.py')
    name = 'natemu-' + uuid.uuid4().hex
    record = dict(schema='natemu.track03.codegen-setup.v1', purpose='exploratory',
                  status='preparing', commands=[], base_image=BASE,
                  started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                  budget=dict(wall_seconds=1800, cpus=2, memory_bytes=8*1024**3,
                              package_download_bytes=700*1024**2))
    deadline = time.monotonic() + 1800
    created = False
    def command(argv, seconds=60):
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('setup wall budget exhausted')
        return retention.command(run, record, argv, timeout=min(seconds, left), cpu_limit=False)
    try:
        retention.save(run, record)
        metadata = json.loads(command(['docker', 'image', 'inspect', BASE]))[0]
        if metadata['Architecture'] != 'arm64' or metadata['Os'] != 'linux':
            raise ValueError('expected pinned Linux ARM64 image')
        created = True  # Unique name permits cleanup even if create times out.
        command(['docker', 'create', '--name', name, '--cpus=2', '--memory=8g',
                 '--memory-swap=8g', '--pids-limit=512', '--user=0',
                 '--tmpfs', '/var/lib/postgresql/data',
                 '--mount', f'type=bind,src={run},dst=/work',
                 '--entrypoint=/bin/sh', BASE, '/work/source/setup.sh'])
        command(['docker', 'start', '--attach', name], 1500)
        state = json.loads(command(['docker', 'inspect', name]))[0]['State']
        if state['Running'] or state['ExitCode'] != 0 or state['OOMKilled']:
            raise RuntimeError('toolchain container failed: ' + json.dumps(state))
        record['toolchain_image'] = command(['docker', 'commit', name], 120).strip()
        command(['docker', 'image', 'inspect', record['toolchain_image']])
        command(['docker', 'save', '-o', str(run / 'toolchain-image.tar'), record['toolchain_image']], 120)
        record['status'] = 'toolchain_prepared_no_generator_built'
    except BaseException as error:
        record['status'] = 'failed'
        record['error'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        if created:
            # The Docker daemon owns this child; killing the CLI is insufficient.
            try:
                retention.command(run, record, ['docker', 'rm', '--force', '--volumes', name], timeout=30, cpu_limit=False)
            except Exception as error:
                record['cleanup_error'] = str(error)
                record['status'] = 'failed_cleanup'
        record['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        retention.save(run, record)
        retention.checksums(run)
        print(run, flush=True)


if __name__ == '__main__':
    main()

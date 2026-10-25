#!/usr/bin/env python3
"""Snapshot, build and check the growing runtime (I1, optionally I2)."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def worker(job_path):
    job = json.loads(job_path.read_text())
    here = job_path.parent
    child_env = dict(os.environ)
    for key in tuple(child_env):
        if key.startswith(('CARGO_', 'RUSTFLAGS', 'RUSTDOC', 'RUSTC', 'RUSTUP_TOOLCHAIN')) or key in ('RUST_ENCODED_FLAGS', 'CFLAGS', 'CXXFLAGS', 'CPPFLAGS', 'LDFLAGS'):
            del child_env[key]
    child_env.update(job['environment'])
    with (here / 'command.stdout').open('xb') as stdout, (here / 'command.stderr').open('xb') as stderr:
        result = subprocess.run(job['argv'], cwd=job['cwd'], stdout=stdout, stderr=stderr, env=child_env)
    if result.returncode != job.get('expected_exit', 0):
        raise RuntimeError(f"child exit {result.returncode}: see {here / 'command.stderr'}")
    if job.get('expected_error'):
        if job['expected_error'] not in (here / 'command.stderr').read_text():
            raise ValueError('wrong rejection diagnostic')
    if job.get('oracle'):
        cell=json.loads((here / 'command.stdout').read_text())
        module(Path(job['oracle']), 'runtime_oracle').check(cell)
        if job.get('producer_check'):
            checker=module(Path(job['producer_check']), 'producer_checker')
            checker.verify_compilations(Path(job['argv'][-1]),cell['requests'])
    print(json.dumps({'status': 'PASS'}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', help='fresh retained run name')
    parser.add_argument('--fragment', type=Path, help='reproduced live_load.arm64')
    parser.add_argument('--snapshot', type=Path, help='rebuild an existing retained source snapshot')
    parser.add_argument('--i2', action='store_true', help='also test the actual FEX producer loop')
    parser.add_argument('--producer', type=Path, help='pinned CodeSizeValidation binary for I2')
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
        return 0
    if not args.name or not args.fragment:
        parser.error('--name and --fragment are required')
    if resource.getrlimit(resource.RLIMIT_CPU) != (resource.RLIM_INFINITY, resource.RLIM_INFINITY):
        parser.error('HVF requires unlimited inherited CPU limits; external wall/RSS supervision bounds execution')
    if args.i2 and not args.producer:
        parser.error('--i2 requires --producer')
    retention = module(REPO / 'experiments/track01/source/run.py', 'retention')
    supervision = module(REPO / 'experiments/track01/run_check.py', 'supervision')
    run = retention.allocate(os.environ.get('NATEMU_RAW_ROOT'), REPO, args.name)
    record = {'format': 'natemu-runtime-run-v2', 'purpose': 'exploratory-development', 'status': 'preparing',
              'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'increments': ['I1','I2'] if args.i2 else ['I1'], 'configured_vcpus': 1, 'tested_vcpus': 1, 'commands': [], 'jobs': [],
              'limits': {'cell_seconds': 20, 'bundle_seconds': 300, 'worker_rss_bytes': 512*1024**2,
                         'build_jobs': 2, 'build_rss_bytes': 8*1024**3, 'run_disk_bytes': 1024**3,
                         'guest_ram_bytes': 1024**2}}
    retention.save(run, record)
    print(run, flush=True)
    try:
        origin = (args.snapshot or REPO).resolve()
        source = run / 'source'
        source.mkdir()
        names = ('Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', '.cargo', 'crates', 'fixtures/i1',
                     'tools/run_i1.py', 'tools/check_i1.py', 'tools/test_run_i1.py', 'experiments/track01/source/run.py',
                     'experiments/track01/run_check.py')
        if args.i2:
            names += ('tools/check_i2.py','tools/fex_worker.py','tools/test_fex_worker.py')
        for name in names:
            path = origin / name
            if path.is_symlink() or (path.is_dir() and any(p.is_symlink() for p in path.rglob('*'))):
                raise ValueError(f'symlink is not a retained input: {path}')
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if path.is_dir():
                shutil.copytree(path, target, ignore=shutil.ignore_patterns('__pycache__'))
            else:
                shutil.copy2(path, target)
        for name in ('tools/run_i1.py', 'experiments/track01/source/run.py', 'experiments/track01/run_check.py'):
            if (source / name).read_bytes() != (REPO / name).read_bytes():
                raise ValueError(f'runner/helper differs from snapshot: {name}; invoke that snapshot runner')
        fragment = args.fragment.resolve().read_bytes()
        metadata = json.loads((source / 'fixtures/i1/live_load.json').read_text())
        if hashlib.sha256(fragment).hexdigest() != metadata['native_sha256'] or fragment.hex() != metadata['native_hex']:
            raise ValueError('fragment differs from pinned producer bundle')
        (run / 'live_load.arm64').write_bytes(fragment)
        if args.i2:
            producer = args.producer.resolve()
            if producer.stat().st_size > 16*1024**2:
                raise ValueError('oversized producer')
            producer_bytes = producer.read_bytes()
            if hashlib.sha256(producer_bytes).hexdigest() != metadata['producer_binary_sha256']:
                raise ValueError('I2 producer differs from retained pinned binary')
            (run / 'CodeSizeValidation').write_bytes(producer_bytes)
            (run / 'CodeSizeValidation').chmod(0o755)
            record['producer_origin'] = str(producer)
        record['source_origin'] = str(origin)
        record['fragment_origin'] = str(args.fragment.resolve())
        record['producer'] = metadata
        for argv in (['sw_vers'], ['uname', '-a'], ['sysctl', 'hw.model', 'hw.ncpu', 'machdep.cpu.brand_string'],
                     ['xcrun', '--show-sdk-version'], ['rustc', '+1.97.0', '-vV'], ['cargo', '+1.97.0', '-V']):
            retention.command(run, record, argv, cpu_limit=False)

        build_env = {'CARGO_HOME': str(run / 'cargo-home'), 'CARGO_TARGET_DIR': str(source / 'target'),
                     'CARGO_BUILD_JOBS': '2', 'RUSTUP_TOOLCHAIN': '1.97.0',
                     'MACOSX_DEPLOYMENT_TARGET': '26.0', 'NATEMU_I2_RUN': args.name}
        record['build_environment'] = build_env
        record['inherited_platform_environment'] = {k: os.environ[k] for k in ('PATH','RUSTUP_HOME','DEVELOPER_DIR','SDKROOT') if k in os.environ}

        def job(label, argv, *, wall=600, rss=8*1024**3, oracle=False, expected_exit=0, expected_error=None):
            directory = run / label
            directory.mkdir()
            data = {'argv': list(map(str, argv)), 'cwd': str(source), 'expected_exit': expected_exit, 'environment': build_env}
            if oracle:
                data['oracle'] = str(source / ('tools/check_i2.py' if oracle == 'i2' else 'tools/check_i1.py'))
            if oracle == 'i2':
                data['producer_check'] = str(source / 'tools/fex_worker.py')
            if expected_error:
                data['expected_error'] = expected_error
            job_path = directory / 'job.json'
            job_path.write_text(json.dumps(data, indent=2)+'\n')
            result = supervision.supervise([sys.executable, '-B', str(source / 'tools/run_i1.py'), '--worker', str(job_path)],
                                          directory, wall=wall, rss_limit=rss, period=.05)
            (directory / 'supervision.json').write_text(json.dumps(result, indent=2)+'\n')
            record['jobs'].append({'name': label, **result})
            retention.save(run, record)
            if result['status'] != 'passed':
                raise RuntimeError(f"{label}: {result['status']}; see retained output")
            total = sum(p.stat().st_size for p in run.rglob('*') if p.is_file())
            if total > record['limits']['run_disk_bytes']:
                raise RuntimeError('retained I1 run exceeded 1GiB disk bound')

        job('tests', ['cargo', '+1.97.0', 'test', '--offline', '--locked'])
        job('worker-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_run_i1.py'])
        if args.i2:
            job('producer-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_fex_worker.py'])
            job('i2-oracle-self-test', [sys.executable, '-B', 'tools/check_i2.py', '--self-test'])
        job('oracle-self-test', [sys.executable, '-B', 'tools/check_i1.py', '--self-test'])
        job('build-host', ['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release', '-p', 'natemu-host'])
        job('build-runtime', ['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release', '-p', 'natemu-runtime', '--target', 'aarch64-unknown-none'])
        host = source / 'target/release/natemu-host'
        runtime = source / 'target/aarch64-unknown-none/release/natemu-runtime'
        job('sign', ['codesign', '--force', '--sign', '-', '--entitlements', 'crates/host/entitlements.plist', host])
        job('verify-signature', ['codesign', '--verify', '--verbose=2', host])
        job('disassembly', ['xcrun', 'llvm-objdump', '-d', runtime])
        record['status'] = 'built'
        retention.save(run, record)
        start = time.monotonic()
        cells = [('control', 0, 'none'), *[('live_load', n, 'none') for n in range(4)], ('fault', 0, 'none'),
                 *[('live_load', 0, mutation) for mutation in ('version','profile','size','target','entry','code','address','vcpu')]]
        for index, (case, vector, mutation) in enumerate(cells):
            remaining = 300 - (time.monotonic()-start)
            if remaining <= 0:
                raise RuntimeError('execution bundle wall bound reached')
            job(f'cell-{index:02d}-{case}-{vector}-{mutation}',
                [host, runtime, run / 'live_load.arm64', case, vector, mutation, 1],
                wall=min(20, remaining), rss=512*1024**2, oracle=True)
        job('unsupported-vcpu-count', [host, runtime, run / 'live_load.arm64', 'control', 0, 'none', 2],
            wall=min(20, max(.01, 300-(time.monotonic()-start))), rss=512*1024**2,
            expected_exit=1, expected_error='I1 admits exactly one vCPU')
        if args.i2:
            start_i2 = time.monotonic()
            scenarios = ('live','source_stale','context_stale','cancel','capacity','bad_reply')
            record['limits'].update(i2_scenario_seconds=120, i2_bundle_seconds=900, compiler_seconds=30, compiler_memory_bytes=512*1024**2)
            for scenario in scenarios:
                remaining = 900-(time.monotonic()-start_i2)
                if remaining <= 0:
                    raise RuntimeError('I2 execution bundle bound reached')
                job('i2-'+scenario, [host,runtime,'i2',scenario,source / 'tools/fex_worker.py',run / 'CodeSizeValidation',run / ('compiler-'+scenario)],
                    wall=min(120, remaining), rss=512*1024**2, oracle='i2')
            record['i2_scenarios_passed'] = len(scenarios)
        record['status'] = 'passed'
        record['cells_passed'] = len(cells)
    except BaseException as error:
        record['status'] = 'failed'
        record['error'] = f'{type(error).__name__}: {error}'
        print(record['error'], file=sys.stderr)
        return 1
    finally:
        record['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        if args.i2:
            try:
                containers = subprocess.check_output(['docker','ps','-aq','--filter',f'label=natemu.i2.run={args.name}'],text=True,timeout=10).split()
                record['remaining_containers'] = containers
                if containers:
                    subprocess.run(['docker','rm','--force','--volumes',*containers],check=True,capture_output=True,timeout=20)
                    record['status'] = 'failed'
                    record['cleanup_error'] = 'compiler containers outlived their workers; forcibly removed'
            except Exception as error:
                record['status'] = 'failed'
                record['cleanup_error'] = str(error)
        retention.save(run, record)
        retention.checksums(run)
    if record['status']=='passed':
        extra=f", {record['i2_scenarios_passed']} live-producer scenarios" if args.i2 else ''
        print(f"PASS: {record['cells_passed']} I1 native cells, unsupported-count rejection{extra}", flush=True)
        return 0
    return 1


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Snapshot, build and check the families runtime's I4 finite profiles (entry, faults, remap, compose, events,
pressure). The I1 entry cells, I2 live translation and I3 paging/allocated cells it once ran are retired: the general
execution path covers them (docs/restructure/slice3-contract.md, S3a2). Its I1 fragment (fixtures/i1) stays the I4
cells' native body; check_i1/check_i2/check_i3_paging stay as the I4 oracle's helpers until I4 retires (S3h)."""
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


def snapshot_inputs(args):
    names = ('Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', '.cargo', 'crates', 'fixtures/i1',
                 'tools/run_i1.py', 'tools/check_i1.py', 'tools/test_run_i1.py', 'experiments/track01/source/run.py',
                 'experiments/track01/run_check.py', 'tools/check_i2.py', 'tools/check_i3_paging.py',
                 'tools/check_i4.py', 'tools/check_i4_pressure.py')
    if args.i4_compose:
        names += ('tools/fex_worker.py','tools/test_fex_worker.py','tools/i4v_producer/ownership.py')
    return names


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
        output = (here / 'command.stdout').read_text()
        cell = ([json.loads(line) for line in output.splitlines()]
                if job.get('oracle_json_lines') else json.loads(output))
        checker=module(Path(job['oracle']), 'runtime_oracle')
        if job.get('i4_expected'):
            checker.check(cell, *job['i4_expected'])
        else:
            checker.check(cell)
        if job.get('producer_check'):
            checker=module(Path(job['producer_check']), 'producer_checker')
            checker.verify_compilations(Path(job['argv'][-1]),cell['requests'])
    print(json.dumps({'status': 'PASS'}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', help='fresh retained run name')
    parser.add_argument('--fragment', type=Path, help='reproduced live_load.arm64')
    parser.add_argument('--snapshot', type=Path, help='rebuild an existing retained source snapshot')
    parser.add_argument('--i4-entry', action='store_true', help='concurrent entry at N=1/2/3/4 (not full I4; the default)')
    parser.add_argument('--i4-faults', action='store_true', help='also test detached native fault queues (not full I4)')
    parser.add_argument('--i4-remap', action='store_true', help='also test concurrent guest remap and local shootdown acknowledgements (not full I4)')
    parser.add_argument('--i4-pressure', action='store_true', help='also test repeated protected writes, bounded queues and real ordinary-work fairness')
    parser.add_argument('--i4-events', action='store_true', help='also test durable pending events during concurrent compile/drain (not full I4)')
    parser.add_argument('--i4-compose', action='store_true', help='also test live source invalidation/publication with retained references (not full I4)')
    parser.add_argument('--producer', type=Path, help='pinned CodeSizeValidation binary (compose, events, pressure)')
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.i4_events = args.i4_events or args.i4_pressure
    args.i4_compose = args.i4_compose or args.i4_events
    args.i4_remap = args.i4_remap or args.i4_compose
    args.i4_faults = args.i4_faults or args.i4_remap
    args.i4_entry = True
    if args.worker:
        worker(args.worker)
        return 0
    if not args.name or not args.fragment:
        parser.error('--name and --fragment are required')
    if resource.getrlimit(resource.RLIMIT_CPU) != (resource.RLIM_INFINITY, resource.RLIM_INFINITY):
        parser.error('HVF requires unlimited inherited CPU limits; external wall/RSS supervision bounds execution')
    if args.i4_compose and not args.producer:
        parser.error('--i4-compose (and events, pressure) requires --producer')
    retention = module(REPO / 'experiments/track01/source/run.py', 'retention')
    supervision = module(REPO / 'experiments/track01/run_check.py', 'supervision')
    run = retention.allocate(os.environ.get('NATEMU_RAW_ROOT'), REPO, args.name)
    record = {'format': 'natemu-runtime-run-v2', 'purpose': 'exploratory-development', 'status': 'preparing',
              'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'increments': [], 'configured_vcpus': 1, 'tested_vcpus': 1, 'commands': [], 'jobs': [],
              'limits': {'cell_seconds': 20, 'bundle_seconds': 300, 'worker_rss_bytes': 512*1024**2,
                         'build_jobs': 2, 'build_rss_bytes': 8*1024**3, 'run_disk_bytes': 1024**3,
                         'guest_ram_bytes': 1024**2}}
    retention.save(run, record)
    if args.i4_entry:
        record['increments'].append('I4-partial')
        record['configured_vcpus'] = 4
        record['tested_vcpus'] = [1,2,3,4]
        record['profile_vcpu_counts'] = {'legacy': [1], 'i4': [1,2,3,4]}
        record['i4_entry_counts'] = [1,2,3,4]
        record['i4_increment_checks_passed'] = False
        record['limits']['guest_ram_bytes'] = 4*1024**2
        record['limits'].update(i4_scenario_seconds=30,bundle_seconds=1200)
    print(run, flush=True)
    try:
        origin = (args.snapshot or REPO).resolve()
        source = run / 'source'
        source.mkdir()
        names = snapshot_inputs(args)
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
        if args.i4_compose:
            producer = args.producer.resolve()
            if producer.stat().st_size > 16*1024**2:
                raise ValueError('oversized producer')
            producer_bytes = producer.read_bytes()
            if hashlib.sha256(producer_bytes).hexdigest() != metadata['producer_binary_sha256']:
                raise ValueError('producer differs from retained pinned binary')
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
                     'MACOSX_DEPLOYMENT_TARGET': '26.0', 'NATEMU_I2_RUN': args.name, 'NATEMU_I3_RUN': args.name}
        record['build_environment'] = build_env
        record['inherited_platform_environment'] = {k: os.environ[k] for k in ('PATH','RUSTUP_HOME','DEVELOPER_DIR','SDKROOT') if k in os.environ}

        execution_deadline=None

        def job(label, argv, *, wall=600, rss=8*1024**3, oracle=False, expected_exit=0, expected_error=None):
            if execution_deadline is not None:
                wall=min(wall,execution_deadline-time.monotonic())
                if wall<=0:
                    raise RuntimeError('combined execution bundle bound reached')
            directory = run / label
            directory.mkdir()
            data = {'argv': list(map(str, argv)), 'cwd': str(source), 'expected_exit': expected_exit, 'environment': build_env}
            if oracle:
                data['oracle'] = str(source / 'tools/check_i4.py')
            if oracle == 'i4':
                data['i4_expected'] = [int(argv[4]), 50 if argv[3] in ('compose','events','pressure') else int(argv[6]), 'i4-'+argv[3]]
                if argv[3] in ('compose','events','pressure'):
                    data['producer_check']=str(source/'tools/fex_worker.py')
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
        if args.i4_compose:
            job('producer-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_fex_worker.py'])
        job('i4-oracle-self-test', [sys.executable, '-B', 'tools/check_i4.py', '--self-test'])
        job('build-host', ['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release', '-p', 'natemu-host'])
        job('build-runtime', ['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release', '-p', 'natemu-runtime', '--target', 'aarch64-unknown-none', '--features', 'families'])
        host = source / 'target/release/natemu-host'
        runtime = source / 'target/aarch64-unknown-none/release/natemu-runtime'
        job('sign', ['codesign', '--force', '--sign', '-', '--entitlements', 'crates/host/entitlements.plist', host])
        job('verify-signature', ['codesign', '--verify', '--verbose=2', host])
        job('disassembly', ['xcrun', 'llvm-objdump', '-d', runtime])
        record['status'] = 'built'
        retention.save(run, record)
        start = time.monotonic()
        execution_deadline=start+record['limits']['bundle_seconds']
        if args.i4_entry:
            for count in (1,2,3,4):
                for delay in (0,50):
                    job(f'i4-entry-n{count}-delay{delay}',
                        [host,runtime,'i4','entry',count,run/'live_load.arm64',delay],
                        wall=30,rss=512*1024**2,oracle='i4')
            for count in (0,5):
                job(f'i4-reject-n{count}', [host,runtime,'i4','entry',count,run/'live_load.arm64',0],
                    wall=30,rss=512*1024**2,expected_exit=1,expected_error='I4 admits 1..=4 vCPUs')
        if args.i4_faults:
            for count in (1,2,3,4):
                job(f'i4-faults-n{count}', [host,runtime,'i4','faults',count,run/'live_load.arm64',50],
                    wall=30,rss=512*1024**2,oracle='i4')
        if args.i4_remap:
            for count in (1,2,3,4):
                job(f'i4-remap-n{count}', [host,runtime,'i4','remap',count,run/'live_load.arm64',50],
                    wall=30,rss=512*1024**2,oracle='i4')
        if args.i4_compose:
            record['limits']['i4_compiler_scenario_seconds']=120
            for count in (1,2,3,4):
                job(f'i4-compose-n{count}', [host,runtime,'i4','compose',count,run/'CodeSizeValidation',run/f'i4-compiler-n{count}'],
                    wall=120,rss=512*1024**2,oracle='i4')
        if args.i4_events:
            for count in (1,2,3,4):
                job(f'i4-events-n{count}',[host,runtime,'i4','events',count,run/'CodeSizeValidation',run/f'i4-events-compiler-n{count}'],
                    wall=120,rss=512*1024**2,oracle='i4')
        if args.i4_pressure:
            for count in (1,2,3,4):
                job(f'i4-pressure-n{count}',[host,runtime,'i4','pressure',count,run/'CodeSizeValidation',run/f'i4-pressure-compiler-n{count}'],
                    wall=120,rss=512*1024**2,oracle='i4')
        if args.i4_pressure:
            record['i4_profile_checks_passed'] = True
            # Execution coverage only; independent acceptance is recorded in
            # the investigation, never inferred by this runner.
        record['status'] = 'passed'
    except BaseException as error:
        record['status'] = 'failed'
        record['error'] = f'{type(error).__name__}: {error}'
        print(record['error'], file=sys.stderr)
        return 1
    finally:
        record['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        if args.i4_compose:
            try:
                containers=[]
                for label in ('natemu.i2.run','natemu.i3.run'):
                    containers.extend(subprocess.check_output(['docker','ps','-aq','--filter',f'label={label}={args.name}'],text=True,timeout=10).split())
                containers=sorted(set(containers))
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
        extra="8 concurrent-entry cells and 2 count rejections"
        if args.i4_faults:
            extra+=", 4 queued-fault cells"
        if args.i4_remap:
            extra+=", 4 remap/shootdown cells"
        if args.i4_compose:
            extra+=", 4 live source-publication cells"
        if args.i4_events:
            extra+=", 4 pending-event cells"
        if args.i4_pressure:
            extra+=", 4 sustained-maintenance cells"
        print(f"PASS: {extra}", flush=True)
        return 0
    return 1


if __name__ == '__main__':
    raise SystemExit(main())

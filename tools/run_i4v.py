#!/usr/bin/env python3
"""Retain the first I4-V native address slice; not full I4-V acceptance."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import shutil
import struct
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    parser.add_argument('--producer', type=Path, required=True)
    parser.add_argument('--stack', action='store_true', help='also execute the stack/prefix and staged-reply slice')
    parser.add_argument('--aliases', action='store_true', help='also execute N=1..4 alias/remap/source composition')
    parser.add_argument('--concurrent-stack', action='store_true', help='also execute N=1..4 stack recovery/source/maintenance composition')
    parser.add_argument('--mixed-aliases', action='store_true', help='also execute N=1..4 parent/leaf permission aliases and terminal fault probes')
    parser.add_argument('--paging-off', action='store_true', help='also execute the live protected32 paging-off slice')
    parser.add_argument('--paging-off-steps', action='store_true', help='also execute protected32 segment/A20 and serviced-route instruction cases')
    parser.add_argument('--mixed-modes', action='store_true', help='also execute paged/paging-off owners with shared source and data')
    parser.add_argument('--controls', action='store_true', help='also execute fetched guest control instructions and legacy32 transitions')
    parser.add_argument('--focus', help='retain only one selected alias/stack concurrency cell for diagnosis')
    args = parser.parse_args()
    selected = [f'{family}-{region}-n{n}' for family,enabled in (('alias',args.aliases),('stack',args.concurrent_stack))
                if enabled for region in ('low','high') for n in (1,2,3,4)]
    if args.mixed_aliases:
        selected += [f'stack-{kind}-{region}-n{n}' for kind in ('parent','leaf') for region in ('low','high') for n in (1,2,3,4)]
    if args.paging_off:
        selected += [f'off32-{region}-{scenario}' for region in ('base','offset') for scenario in ('cold','readonly','warm')]
    step_scenarios = ('segments','a20-off','ds-limit','cs-before-load','cs-inside-load','cs-first','ds-readonly','device','hole','linear-wrap')
    if args.paging_off_steps:
        selected += ['off-steps-'+scenario for scenario in step_scenarios]
    if args.mixed_modes:
        selected += [f'mixed-{arrangement}-n{n}' for arrangement in ('off-first','paged-first') for n in (1,2,3,4)]
    control_scenarios = ('roundtrip','reload','wp','bad-pg','bad-nw','bad-cr4','privilege','fetch-np','ignored-cr3','operand-mod','lock','reserved-cr','pge',
                         'pae-activate','pae-invlpg','pae-eviction','pae-wp-pge','pae-bad-activate','pae-bad-cr3','pae-bad-cr4','pae-nonpresent','pae-aba',
                         'long-direct','long-via-pae','long-aba','long-bad-lme','long-no-pae','long-clear-pae','long-clear-pg','long-bad-efer','long-ea64','long-gdt-limit','long-not-present','long-ld','long-pointer-np','long-target-np','long-tss16','long-data-selector','long-target-noncanonical')
    if args.controls:
        selected += ['controls-'+name for name in control_scenarios]
    if args.focus and args.focus not in selected:
        parser.error('--focus must name a cell enabled by its selected profile')
    if resource.getrlimit(resource.RLIMIT_CPU) != (resource.RLIM_INFINITY,) * 2:
        parser.error('HVF requires unlimited inherited CPU limits; external supervision bounds runs')
    retention = module(REPO / 'experiments/track01/source/run.py', 'retention')
    supervisor = module(REPO / 'experiments/track01/run_check.py', 'supervisor')
    run = retention.allocate(os.environ.get('NATEMU_RAW_ROOT'), REPO, args.name)
    record = dict(format='natemu-i4v-run-v1', purpose='exploratory-development',
                  status='preparing', full_i4v_complete=False, tested_vcpus=[],
                  started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                  source_origin=str(REPO), jobs=[], commands=[], compilations=[], cells=[],
                  stack_enabled=args.stack,
                  aliases_enabled=args.aliases,
                  concurrent_stack_enabled=args.concurrent_stack,
                  mixed_aliases_enabled=args.mixed_aliases,
                  paging_off_enabled=args.paging_off,
                  paging_off_steps_enabled=args.paging_off_steps,
                  mixed_modes_enabled=args.mixed_modes,
                  controls_enabled=args.controls,
                  focus=args.focus,
                  limits=dict(build_jobs=2, build_rss_bytes=8*1024**3,
                              worker_rss_bytes=512*1024**2, cell_seconds=120,
                              bundle_seconds=1200, run_disk_bytes=2*1024**3))
    retention.save(run, record)
    print(run, flush=True)
    try:
        source = run / 'source'
        source.mkdir()
        for name in ('Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', '.cargo', 'crates',
                     'fixtures/i4v', 'tools/i4v_producer', 'tools/run_i4v.py',
                     'tools/i4v_worker.py', 'tools/test_i4v_worker.py',
                     'tools/i4v_stack_worker.py', 'tools/test_i4v_stack_worker.py',
                     'tools/i4v_off_worker.py', 'tools/test_i4v_off_worker.py',
                     'tools/i4v_off_check.py', 'tools/test_i4v_off_check.py',
                     'tools/i4v_off_steps_worker.py', 'tools/test_i4v_off_steps_worker.py',
                     'tools/i4v_off_steps_check.py', 'tools/test_i4v_off_steps_check.py',
                     'tools/i4v_mixed32_worker.py', 'tools/test_i4v_mixed32_worker.py',
                     'tools/check_i4v_mixed.py', 'tools/test_check_i4v_mixed.py',
                     'tools/i4v_controls_worker.py', 'tools/test_i4v_controls_worker.py',
                     'tools/i4v_controls_check.py', 'tools/test_i4v_controls_check.py',
                     'tools/i4v_pae_model.py',
                     'tools/i4v_modes_model.py',
                     'tools/i4v_stack_check.py', 'tools/test_i4v_stack_check.py',
                     'tools/i4v_alias_worker.py', 'tools/test_i4v_alias_worker.py',
                     'tools/check_i4v_alias.py', 'tools/test_check_i4v_alias.py',
                     'tools/check_i4v_stack_concurrent.py', 'tools/test_check_i4v_stack_concurrent.py',
                     'tools/check_i4.py', 'tools/check_i4_pressure.py', 'tools/check_i1.py',
                     'tools/check_i2.py', 'tools/check_i3_paging.py',
                     'tools/run_i1.py', 'tools/i4v_check.py', 'tools/test_i4v_check.py',
                     'experiments/track01/source/run.py', 'experiments/track01/run_check.py'):
            origin, target = REPO / name, source / name
            if origin.is_symlink() or (origin.is_dir() and any(p.is_symlink() for p in origin.rglob('*'))):
                raise ValueError(f'symlink source input: {name}')
            target.parent.mkdir(parents=True, exist_ok=True)
            if origin.is_dir():
                shutil.copytree(origin, target, ignore=shutil.ignore_patterns('__pycache__'))
            else:
                shutil.copy2(origin, target)
        pin = json.loads((source / 'tools/i4v_producer/producer.json').read_text())
        producer = run / 'CodeSizeValidation'
        if args.producer.stat().st_size > 16*1024**2:
            raise ValueError('oversized producer')
        producer_bytes = args.producer.read_bytes()
        if hashlib.sha256(producer_bytes).hexdigest() != pin['binary_sha256']:
            raise ValueError('producer does not match retained pin')
        producer.write_bytes(producer_bytes)
        producer.chmod(0o755)
        record['producer_origin'] = str(args.producer.resolve())
        record['producer_sha256'] = pin['binary_sha256']
        for argv in (['sw_vers'], ['uname', '-a'], ['sysctl', 'hw.model', 'hw.ncpu'],
                     ['xcrun', '--show-sdk-version'], ['rustc', '+1.97.0', '-vV']):
            retention.command(run, record, argv, cpu_limit=False)
        environment = dict(CARGO_HOME=str(run / 'cargo-home'), CARGO_TARGET_DIR=str(source / 'target'),
                           CARGO_BUILD_JOBS='2', RUSTUP_TOOLCHAIN='1.97.0', MACOSX_DEPLOYMENT_TARGET='26.0')
        record['build_environment'] = environment
        deadline = time.monotonic() + record['limits']['bundle_seconds']

        def job(label, argv, oracle=False, wall=120, rss=512*1024**2):
            directory = run / label
            directory.mkdir()
            specification = dict(argv=list(map(str, argv)), cwd=str(source), environment=environment)
            if oracle:
                specification['oracle'] = str(source / ('tools/i4v_controls_check.py' if oracle == 'controls'
                    else 'tools/check_i4v_mixed.py' if oracle == 'mixed'
                    else 'tools/i4v_off_steps_check.py' if oracle == 'off-steps'
                    else 'tools/i4v_off_check.py' if oracle == 'off32'
                    else 'tools/i4v_stack_check.py' if oracle == 'stack'
                    else 'tools/check_i4v_alias.py' if oracle == 'aliases'
                    else 'tools/check_i4v_stack_concurrent.py' if oracle == 'concurrent-stack' else 'tools/i4v_check.py'))
            path = directory / 'job.json'
            path.write_text(json.dumps(specification, indent=2)+'\n')
            remaining = min(wall, deadline - time.monotonic())
            if remaining <= 0:
                raise RuntimeError('I4-V execution bundle bound reached')
            result = supervisor.supervise(
                [sys.executable, '-B', str(source / 'tools/run_i1.py'), '--worker', str(path)],
                directory, wall=remaining, rss_limit=rss, period=.05)
            (directory / 'supervision.json').write_text(json.dumps(result, indent=2)+'\n')
            record['jobs'].append(dict(name=label, **result))
            retention.save(run, record)
            if result['status'] != 'passed':
                raise RuntimeError(f'{label}: {result["status"]}; inspect retained output')
            if sum(p.stat().st_size for p in run.rglob('*') if p.is_file()) > record['limits']['run_disk_bytes']:
                raise RuntimeError('I4-V retained run disk bound reached')
            return directory

        job('rust-tests', ['cargo', '+1.97.0', 'test', '--offline', '--locked'], wall=600, rss=8*1024**3)
        for label, folder, pattern in (('producer-tests', 'tools/i4v_producer', 'test_*.py'),
                                       ('expectation-tests', 'fixtures/i4v', 'test_*.py'),
                                       ('oracle-tests', 'tools', 'test_i4v_check.py'),
                                       ('worker-tests', 'tools', 'test_i4v_worker.py'),
                                       ('stack-oracle-tests', 'tools', 'test_i4v_stack_check.py'),
                                       ('stack-worker-tests', 'tools', 'test_i4v_stack_worker.py'),
                                       ('alias-oracle-tests', 'tools', 'test_check_i4v_alias.py'),
                                       ('alias-worker-tests', 'tools', 'test_i4v_alias_worker.py')):
            job(label, [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', folder, '-p', pattern])
        job('concurrent-stack-oracle-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_check_i4v_stack_concurrent.py'])
        job('off32-worker-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_i4v_off_worker.py'])
        job('off32-oracle-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_i4v_off_check.py'])
        job('off-steps-worker-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_i4v_off_steps_worker.py'])
        job('off-steps-oracle-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_i4v_off_steps_check.py'])
        job('mixed-worker-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_i4v_mixed32_worker.py'])
        job('mixed-oracle-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_check_i4v_mixed.py'])
        job('controls-worker-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_i4v_controls_worker.py'])
        job('controls-oracle-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_i4v_controls_check.py'])
        job('build-host', ['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release', '-p', 'natemu-host'], wall=600, rss=8*1024**3)
        job('build-runtime', ['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release', '-p', 'natemu-runtime', '--target', 'aarch64-unknown-none'], wall=600, rss=8*1024**3)
        host = source / 'target/release/natemu-host'
        runtime = source / 'target/aarch64-unknown-none/release/natemu-runtime'
        job('sign', ['codesign', '--force', '--sign', '-', '--entitlements', 'crates/host/entitlements.plist', host])
        job('verify-signature', ['codesign', '--verify', '--verbose=2', host])
        exporter = module(source / 'tools/i4v_producer/export.py', 'exporter')
        for region, rip in (() if args.focus else (('high', 0xffff800000102000), ('low', 0x102000))):
            directory = run / ('compile-' + region)
            job('producer-' + region, [sys.executable, '-B', source / 'tools/i4v_producer/compile.py',
                                       '--directory', directory, '--producer', producer,
                                       '--case', 'fex_memory_data64', '--rip', hex(rip)])
            exported = exporter.verify(directory, expected={'case': 'fex_memory_data64', 'guest_rip': rip})
            record['compilations'].append(dict(directory=directory.name, export=exported))
            for scenario in ('cold', 'load_np', 'user_denied', 'fetch_nx', 'smep', 'low_operand', 'private_operand'):
                label = region + '-' + scenario
                result = job(label, [host, runtime, 'i4v', directory / 'body.bin', scenario, hex(rip), '1'], oracle=True)
                report = json.loads((result / 'command.stdout').read_text())
                if report['scenario'] != scenario or report['live'] != 0 or report['reply_mutation'] != -1 or report['rip'] != rip or report['body_hex'] != exported['body_hex']:
                    raise ValueError('runtime does not match actual compiled request')
                record['cells'].append(dict(name=label, compilation=directory.name))
        live_cells = [(region, rip, scenario, None)
                      for region, rip in (('high', 0xffff800000102000), ('low', 0x102000))
                      for scenario in ('cold', 'load_np', 'user_denied', 'fetch_nx', 'smep', 'low_operand', 'private_operand')]
        live_cells += [('high', 0xffff800000102000, 'cold', i) for i in range(16)]
        if args.focus:
            live_cells = []
        for region, rip, scenario, mutation in live_cells:
            label = f'live-{region}-{scenario}' + (f'-reply-{mutation}' if mutation is not None else '')
            output = run / (label + '-producer')
            argv = [host, runtime, 'i4v-live', producer, scenario, hex(rip), '1',
                    source / 'tools/i4v_worker.py', output]
            if mutation is not None:
                argv.append(str(mutation))
            result = job(label, argv, oracle=True)
            report = json.loads((result / 'command.stdout').read_text())
            expected_requests = int(scenario not in ('fetch_nx', 'smep', 'private_operand'))
            if (report['live'] != 1 or report['requests'] != expected_requests or report['rip'] != rip
                    or report['scenario'] != scenario or report['reply_mutation'] != (-1 if mutation is None else mutation)):
                raise ValueError('live runtime request mode/count/identity mismatch')
            directories = list(output.glob('request-*'))
            if len(directories) != expected_requests:
                raise ValueError('runtime requests and actual compilations differ')
            for directory in directories:
                exported = exporter.verify(directory / 'compile', expected={'case': 'fex_memory_data64', 'guest_rip': rip})
                request = list(struct.unpack('<16Q', (directory / 'request.bin').read_bytes()))
                if request != report['request'] or exported['body_hex'] != report['body_hex']:
                    raise ValueError('resident request/actual compile association mismatch')
                reply = list(struct.unpack('<16Q', (directory / 'reply.bin').read_bytes()))
                if mutation is not None:
                    reply[mutation] ^= 1
                if reply != report['reply']:
                    raise ValueError('runtime reply differs from producer delivery/control mutation')
                record['compilations'].append(dict(directory=str(directory.relative_to(run)), export=exported))
            record['cells'].append(dict(name=label, producer=str(output.relative_to(run)), reply_mutation=mutation))
        if args.stack and not args.focus:
            # The verifier's imports must come from the retained snapshot too.
            sys.path.insert(0, str(source / 'tools'))
            stack_worker = module(source / 'tools/i4v_stack_worker.py', 'stack_worker')
            sys.path.pop(0)
            scenarios = ('cold', 'low_stack', 'ro_first', 'ro_after_first', 'np_after_first',
                         'wp_off', 'user_ro', 'user_denied', 'user_ok', 'parent_ro',
                         'fetch_nx', 'smep', 'ss_first', 'ss_after_first', 'ss_span',
                         'gp_after_pushes', 'read_np_after_pushes')
            stack_cells = [(region, rip, scenario, -1)
                           for region, rip in (('high', 0xffff800000102000), ('low', 0x102000))
                           for scenario in scenarios]
            stack_cells += [('high', 0xffff800000102000, 'cold', i) for i in range(48)]
            for region, rip, scenario, mutation in stack_cells:
                label = f'stack-{region}-{scenario}' + (f'-reply-{mutation}' if mutation >= 0 else '')
                output = run / (label + '-producer')
                argv = [host, runtime, 'i4v-stack-live', producer, scenario, hex(rip), '1',
                        source / 'tools/i4v_stack_worker.py', output]
                if mutation >= 0:
                    argv.append(str(mutation))
                result = job(label, argv, oracle='stack')
                report = json.loads((result / 'command.stdout').read_text())
                expected_requests = int(scenario not in ('fetch_nx', 'smep', 'ss_first', 'ss_span'))
                transfers = 0 if not expected_requests else mutation//16+1 if mutation >= 0 else 3
                if (report['case'] != 'fex_push_immediates64' or report['live'] != 1
                        or report['scenario'] != scenario or report['rip'] != rip
                        or report['reply_mutation'] != mutation or report['requests'] != expected_requests
                        or report['stack']['transfers'] != transfers):
                    raise ValueError('stack runtime does not match requested cell/transfer count')
                directories = list(output.glob('request-*'))
                if len(directories) != expected_requests:
                    raise ValueError('stack requests and compilations differ')
                for directory in directories:
                    exported = stack_worker.verify_transfers(directory,
                        report['stack']['requests'][:transfers], report['stack']['replies'][:transfers])
                    if exported['guest_rip'] != rip or exported['body_hex'] != report['body_hex']:
                        raise ValueError('stack compilation differs from actual requested RIP/body')
                    record['compilations'].append(dict(directory=str(directory.relative_to(run)), export=exported))
                record['cells'].append(dict(name=label, producer=str(output.relative_to(run)), reply_mutation=mutation))
        if args.paging_off:
            sys.path.insert(0, str(source / 'tools'))
            off_worker = module(source / 'tools/i4v_off_worker.py', 'off_worker')
            sys.path.pop(0)
            cells = [(region,rip,scenario,-1) for region,rip in (('base',0x102000),('offset',0x102080))
                     for scenario in ('cold','readonly','warm')]
            if not args.focus:
                cells += [('base',0x102000,'cold',mutation) for mutation in range(48)]
            for region,rip,scenario,mutation in cells:
                label = f'off32-{region}-{scenario}' + (f'-reply-{mutation}' if mutation >= 0 else '')
                if args.focus and label != args.focus:
                    continue
                output = run / (label+'-producer')
                argv = [host,runtime,'i4v-off-live',producer,scenario,hex(rip),'1',
                        source / 'tools/i4v_off_worker.py',output]
                if mutation >= 0:
                    argv.append(str(mutation))
                result = job(label,argv,oracle='off32')
                report = json.loads((result/'command.stdout').read_text())
                transfers = mutation//16+1 if mutation >= 0 else 3
                if (report['case'] != 'fex_moffs_load32' or report['live'] != 1
                        or report['scenario'] != scenario or report['rip'] != rip
                        or report['reply_mutation'] != mutation or report['requests'] != 1
                        or report['off']['transfers'] != transfers):
                    raise ValueError('paging-off report differs from requested identity/transfers')
                directories = list(output.glob('request-*'))
                if len(directories) != 1:
                    raise ValueError('paging-off requests and compilations differ')
                for directory in directories:
                    exported = off_worker.verify_transfers(directory,
                        report['off']['requests'][:transfers],report['off']['replies'][:transfers])
                    if exported['guest_rip'] != rip or exported['bitness'] != 32 or exported['body_hex'] != report['body_hex']:
                        raise ValueError('paging-off compile differs from actual RIP/mode/body')
                    record['compilations'].append(dict(directory=str(directory.relative_to(run)),export=exported))
                record['cells'].append(dict(name=label,producer=str(output.relative_to(run)),reply_mutation=mutation))
        if args.paging_off_steps:
            sys.path.insert(0, str(source / 'tools'))
            steps_worker = module(source / 'tools/i4v_off_steps_worker.py', 'steps_worker')
            sys.path.pop(0)
            step_cells = [(scenario,-1) for scenario in step_scenarios]
            if not args.focus:
                step_cells += [('segments',mutation) for mutation in range(25)]
            for scenario,mutation in step_cells:
                label = 'off-steps-'+scenario + (f'-reply-{mutation}' if mutation >= 0 else '')
                if args.focus and label != args.focus:
                    continue
                output = run / (label+'-producer')
                argv = [host,runtime,'i4v-off-steps',producer,scenario,'1',
                        source / 'tools/i4v_off_steps_worker.py',output]
                if mutation >= 0:
                    argv.append(str(mutation))
                result = job(label,argv,oracle='off-steps')
                report = json.loads((result/'command.stdout').read_text())
                transfers = report['steps']['transfers']
                if (report['case'] != 'fex_moffs_load32_steps' or report['live'] != 1
                        or report['scenario'] != scenario or report['rip'] != 0x2000
                        or report['reply_mutation'] != mutation or report['requests'] != transfers
                        or report['steps']['profile'] != step_scenarios.index(scenario)):
                    raise ValueError('instruction report differs from requested identity/transfers')
                requests = report['steps']['requests'][:transfers]
                replies = report['steps']['replies'][:transfers]
                steps_worker.verify_compilations(output,requests,replies)
                for index,request in enumerate(requests):
                    directory = output / f'request-{index:03}'
                    exported = steps_worker.verify_request(directory,request)
                    if exported['guest_rip'] != request[7] or exported['bitness'] != 32:
                        raise ValueError('instruction compile differs from actual EIP/mode')
                    record['compilations'].append(dict(directory=str(directory.relative_to(run)),export=exported))
                record['cells'].append(dict(name=label,producer=str(output.relative_to(run)),reply_mutation=mutation))
        if args.controls:
            sys.path.insert(0,str(source/'tools'))
            controls_worker = module(source/'tools/i4v_controls_worker.py','controls_worker')
            sys.path.pop(0)
            for profile,scenario in enumerate(control_scenarios):
                label='controls-'+scenario
                if args.focus and label != args.focus:
                    continue
                output=run/(label+'-producer')
                result=job(label,[host,runtime,'i4v-controls',producer,str(profile),'1',source/'tools/i4v_controls_worker.py',output],oracle='controls')
                report=json.loads((result/'command.stdout').read_text())
                if report['case'] != 'controls32' or report['controls']['profile'] != profile:
                    raise ValueError('control report differs from requested program')
                pairs=[(v['request'],v['reply']) for v in report['visits'] if v['entries']]
                if profile in (0,21,24):
                    pairs.append((report['controls']['deferred'],report['controls']['late_reply']))
                pairs.sort(key=lambda pair:pair[0][12])
                requests=[p[0] for p in pairs];replies=[p[1] for p in pairs]
                controls_worker.verify_compilations(output,requests,replies)
                if requests != report['compiler_requests'] or len(requests) != report['requests']:
                    raise ValueError('control actual compiler sequence differs from resident records')
                for index,request in enumerate(requests):
                    directory=output/f'request-{index:03}'
                    exported=controls_worker.verify_request(directory,request)
                    if exported['guest_rip'] != request[7] or exported['bitness'] != (64 if request[8] == 4 else 32):
                        raise ValueError('control ordinary compilation EIP/mode differs')
                    record['compilations'].append(dict(directory=str(directory.relative_to(run)),export=exported))
                record['cells'].append(dict(name=label,producer=str(output.relative_to(run))))
        if args.mixed_modes:
            sys.path.insert(0, str(source / 'tools'))
            mixed_worker = module(source / 'tools/i4v_mixed32_worker.py', 'mixed_worker')
            sys.path.pop(0)
            for arrangement in ('off-first','paged-first'):
                for n in (1,2,3,4):
                    label = f'mixed-{arrangement}-n{n}'
                    if args.focus and label != args.focus:
                        continue
                    output = run / (label+'-producer')
                    result = job(label, [host,runtime,'i4v-mixed',arrangement,str(n),producer,output], oracle='mixed')
                    report = json.loads((result/'command.stdout').read_text())
                    if (report['profile'] != 'i4-'+arrangement or report['configured_vcpus'] != n
                            or report['tested_vcpus'] != n or report['mixed_profile'] != (1 if arrangement == 'off-first' else 2)):
                        raise ValueError('mixed report differs from requested owner/mode profile')
                    associated = []
                    for owner in report['mixed']:
                        requests, replies = owner['requests'], owner['replies']
                        root = output / f'cpu-{owner["id"]}'
                        mixed_worker.verify_compilations(root,requests,replies)
                        associated += requests
                        for index, request in enumerate(requests):
                            directory = root / f'request-{index:03}'
                            exported = mixed_worker.verify_request(directory,request)
                            if exported['guest_rip'] != request[7] or exported['bitness'] != 32:
                                raise ValueError('mixed compile differs from owner EIP/mode')
                            record['compilations'].append(dict(directory=str(directory.relative_to(run)),export=exported))
                    if associated != report['requests'] or len(list(output.glob('cpu-*'))) != n:
                        raise ValueError('mixed owner compilation association mismatch')
                    record['cells'].append(dict(name=label,producer=str(output.relative_to(run)),vcpus=n))
        if args.aliases:
            sys.path.insert(0, str(source / 'tools'))
            alias_worker = module(source / 'tools/i4v_alias_worker.py', 'alias_worker')
            sys.path.pop(0)
            for profile, scenario, rip in ((1,'alias-low',0x102000), (2,'alias-high',0xffff800000102000)):
                for n in (1,2,3,4):
                    label = f'{scenario}-n{n}'
                    if args.focus and label != args.focus:
                        continue
                    output = run / (label+'-producer')
                    result = job(label, [host, runtime, 'i4', scenario, str(n), producer, output], oracle='aliases')
                    report = json.loads((result/'command.stdout').read_text())
                    if (report['profile'] != 'i4-'+scenario or report['alias_profile'] != profile
                            or report['source_rip'] != rip or report['configured_vcpus'] != n
                            or report['tested_vcpus'] != n or report['delayed_owner_ms'] != 50):
                        raise ValueError('alias report differs from requested N/address profile')
                    alias_worker.verify_compilations(output, report['requests'])
                    for index, request in enumerate(report['requests']):
                        directory = output / f'request-{index:03}'
                        exported = alias_worker.verify_request(directory, request)
                        if exported['guest_rip'] != rip:
                            raise ValueError('alias compilation does not use requested full RIP')
                        record['compilations'].append(dict(directory=str(directory.relative_to(run)), export=exported))
                    record['cells'].append(dict(name=label, producer=str(output.relative_to(run)), vcpus=n))
        if args.concurrent_stack or args.mixed_aliases:
            sys.path.insert(0, str(source / 'tools'))
            concurrent_worker = module(source / 'tools/i4v_stack_worker.py', 'concurrent_stack_worker')
            sys.path.pop(0)
            profiles = [(1,profile,'stack-'+region,rip) for profile,region,rip in
                        ((1,'low',0x102000),(2,'high',0xffff800000102000))] if args.concurrent_stack else []
            if args.mixed_aliases:
                profiles += [(kind,profile,f'stack-{family}-{region}',rip) for kind,family in ((2,'parent'),(3,'leaf'))
                             for profile,region,rip in ((1,'low',0x102000),(2,'high',0xffff800000102000))]
            for kind, profile, scenario, rip in profiles:
                for n in (1,2,3,4):
                    label = f'{scenario}-n{n}'
                    if args.focus and label != args.focus:
                        continue
                    output = run / (label+'-producer')
                    result = job(label, [host, runtime, 'i4', scenario, str(n), producer, output], oracle='concurrent-stack')
                    report = json.loads((result/'command.stdout').read_text())
                    if (report['profile'] != 'i4-'+scenario or report['alias_profile'] != profile
                            or report['stack_profile'] != kind or report['source_rip'] != rip
                            or report['configured_vcpus'] != n or report['tested_vcpus'] != n
                            or report['delayed_owner_ms'] != 50):
                        raise ValueError('concurrent stack report differs from requested profile')
                    concurrent_worker.verify_compilations(output, report['requests'])
                    for index, request in enumerate(report['requests']):
                        directory = output / f'request-{index:03}'
                        exported = concurrent_worker.verify_request(directory, request)
                        compiler_owner = next(owner for owner in report['stacks'] if owner['id'] == 0)
                        concurrent_worker.verify_transfers(directory,
                            concurrent_worker.requests_for(tuple(request)), compiler_owner['replies'][index])
                        if exported['guest_rip'] != rip:
                            raise ValueError('concurrent stack compilation uses wrong full RIP')
                        record['compilations'].append(dict(directory=str(directory.relative_to(run)),export=exported))
                    record['cells'].append(dict(name=label,producer=str(output.relative_to(run)),vcpus=n))
        record['tested_vcpus'] = sorted({cell.get('vcpus',1) for cell in record['cells']})
        record['status'] = 'passed'
    except BaseException as error:
        record.update(status='failed', error=str(error))
        raise
    finally:
        record['tested_vcpus'] = sorted({cell.get('vcpus',1) for cell in record['cells']})
        # An externally terminated compiler worker may not reach its cleanup.
        for path in run.rglob('compiler.json'):
            if source in path.parents:
                continue
            compiler = json.loads(path.read_text())
            commands = compiler.get('commands', [])
            if commands and not any(c['argv'][:2] == ['docker', 'rm'] and c.get('returncode') == 0 for c in commands):
                argv = commands[0]['argv']
                name = argv[argv.index('--name')+1]
                cleanup = subprocess.run(['docker', 'rm', '--force', '--volumes', name], capture_output=True, text=True, timeout=30)
                record.setdefault('emergency_cleanup', []).append(dict(container=name, returncode=cleanup.returncode, stderr=cleanup.stderr))
                record['status'] = 'failed'
        record['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        retention.save(run, record)
        retention.checksums(run)
    if record['status'] != 'passed':
        raise RuntimeError('I4-V cleanup failed; see retained manifest')
    print(f'PASS {len(record["cells"])} native address cells; full I4-V remains incomplete')


if __name__ == '__main__':
    main()

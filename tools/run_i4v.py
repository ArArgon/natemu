#!/usr/bin/env python3
"""Retain the I4-V native family campaigns; not full I4-V acceptance. The address (eager and live high/low cells,
reply-word flips) and stack (17 scenarios, flips) slices it began with are retired: the general execution path
covers them (docs/restructure/slice3-contract.md, S3a2)."""
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


def bind_partial_store(report, expected_pc):
    """Bind populated fault sites to the independently audited producing ELF."""
    if report['case']!='partial-progress':return
    for owner in report['owners']:
        if owner['faults'] not in (1,2) or owner['store_pc']!=expected_pc:
            raise ValueError('partial helper store differs from producing ELF')
        if any(fault['pc']!=expected_pc for fault in owner['fault'][:owner['faults']]):
            raise ValueError('partial helper fault differs from producing ELF')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    parser.add_argument('--producer', type=Path, required=True)
    parser.add_argument('--aliases', action='store_true', help='also execute N=1..4 alias/remap/source composition')
    parser.add_argument('--concurrent-stack', action='store_true', help='also execute N=1..4 stack recovery/source/maintenance composition')
    parser.add_argument('--mixed-aliases', action='store_true', help='also execute N=1..4 parent/leaf permission aliases and terminal fault probes')
    parser.add_argument('--paging-off', action='store_true', help='also execute the live protected32 paging-off slice')
    parser.add_argument('--paging-off-steps', action='store_true', help='also execute protected32 segment/A20 and serviced-route instruction cases')
    parser.add_argument('--mixed-modes', action='store_true', help='also execute paged/paging-off owners with shared source and data')
    parser.add_argument('--controls', action='store_true', help='also execute fetched guest control instructions and legacy32 transitions')
    parser.add_argument('--concurrent-controls', action='store_true', help='also compose guest controls with retained peer work at N=1..4')
    parser.add_argument('--stack-boundaries', action='store_true', help='also execute terminal stack faults and checked split stores at N=1..4')
    parser.add_argument('--address-forms', action='store_true', help='also execute segmented protected32 address formation at N=1..4')
    parser.add_argument('--fence', action='store_true', help='execute full live-result LFENCE dependency cases')
    parser.add_argument('--ordering', action='store_true', help='execute native litmus, XCHG fault/retry and fence controls')
    parser.add_argument('--coherence', action='store_true', help='execute parent, A/D and page-size coherence at N=1..4')
    parser.add_argument('--progress', action='store_true', help='execute seeded stack/control/coherence schedules')
    parser.add_argument('--containment', action='store_true', help='execute 25 injected failures and fresh control VMs')
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
    concurrent_profiles = ((0, 'legacy'), (21, 'pae'), (24, 'long-aba'), (22, 'long-direct'), (3, 'rejected'))
    if args.concurrent_controls:
        selected += [f'controls-concurrent-{name}-n{n}-actor{actor}' for _, name in concurrent_profiles for n in (1,2,3,4) for actor in range(n)]
    boundary_profiles = tuple(enumerate(('ss','gp','split','split-np','split-ro'), 1))
    boundary_cases = [(profile, name, n, actor, 'high' if (profile+actor)%2 else 'low')
                      for profile,name in boundary_profiles for n in (1,2,3,4) for actor in range(n)]
    if args.stack_boundaries:
        selected += [f'boundary-{name}-n{n}-actor{actor}-{region}' for _,name,n,actor,region in boundary_cases]
    address_profiles = tuple(enumerate(('subpage','a20-off','linear-wrap','address16','ds-boundary','cs-boundary')))
    if args.address_forms:
        selected += [f'address-{name}-n{n}-actor{actor}' for _,name in address_profiles for n in (1,2,3,4) for actor in range(n)]
    ordering_names=('sb','mp','lb','wrc','iriw','sb-mfence','sb-xchg','xchg','xchg-pre','xchg-post','lfence','sfence','mfence')
    ordering_cells=[(profile,name,n,rotation,seed)
        for profile,name in enumerate(ordering_names) for n in range(1,5)
        if profile>=7 or n==(2,2,2,3,4,2,2)[profile]
        for rotation in range(n) for seed in (1,37)]
    if args.ordering:
        selected += [f'ordering-{name}-n{n}-rotation{rotation}-seed{seed}' for _,name,n,rotation,seed in ordering_cells]
    if args.fence:
        selected += [f'fence-case{case}-n{n}' for case in range(5) for n in range(1,5)]
    coherence_cells = [(profile, name, n, actor)
        for profile, name in enumerate(('parent', 'ad-legacy', 'ad-pae', 'ad-four', 'size-pae', 'size-four'))
        for n in range(1, 5) for actor in range(n)]
    if args.coherence:
        selected += [f'coherence-{name}-n{n}-actor{actor}' for _, name, n, actor in coherence_cells]
    progress_cells=[(family,n,ordinal) for family in ('stack','controls','coherence','atomic','partial') for n in range(1,5) for ordinal in range(2 if family=='partial' else 4)]
    if args.progress:
        selected += [f'progress-{family}-n{n}-seed{ordinal}' for family,n,ordinal in progress_cells]
    containment_cells=[(failure,n,actor) for failure in range(1,6) for n,actor in ((1,0),(2,0),(2,1),(4,0),(4,3))]
    if args.containment:
        selected += [f'containment-f{failure}-n{n}-actor{actor}' for failure,n,actor in containment_cells]
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
                  standalone_compilers={},
                  aliases_enabled=args.aliases,
                  concurrent_stack_enabled=args.concurrent_stack,
                  mixed_aliases_enabled=args.mixed_aliases,
                  paging_off_enabled=args.paging_off,
                  paging_off_steps_enabled=args.paging_off_steps,
                  mixed_modes_enabled=args.mixed_modes,
                  controls_enabled=args.controls,
                  concurrent_controls_enabled=args.concurrent_controls,
                  stack_boundaries_enabled=args.stack_boundaries,
                  address_forms_enabled=args.address_forms,
                  ordering_enabled=args.ordering,
                  fence_enabled=args.fence,
                  coherence_enabled=args.coherence,
                  progress_enabled=args.progress,
                  containment_enabled=args.containment,
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
                     'tools/i4v_stack_worker.py', 'tools/test_i4v_stack_worker.py',
                     'tools/i4v_off_worker.py', 'tools/test_i4v_off_worker.py',
                     'tools/i4v_off_check.py', 'tools/test_i4v_off_check.py',
                     'tools/i4v_off_steps_worker.py', 'tools/test_i4v_off_steps_worker.py',
                     'tools/i4v_off_steps_check.py', 'tools/test_i4v_off_steps_check.py',
                     'tools/i4v_mixed32_worker.py', 'tools/test_i4v_mixed32_worker.py',
                     'tools/check_i4v_mixed.py', 'tools/test_check_i4v_mixed.py',
                     'tools/i4v_controls_worker.py', 'tools/test_i4v_controls_worker.py',
                     'tools/i4v_controls_check.py', 'tools/test_i4v_controls_check.py',
                     'tools/i4v_controls_concurrent_check.py', 'tools/test_i4v_controls_concurrent_check.py',
                     'tools/i4v_pae_model.py',
                     'tools/i4v_modes_model.py',
                     'tools/i4v_stack_check.py', 'tools/test_i4v_stack_check.py',
                     'tools/i4v_alias_worker.py', 'tools/test_i4v_alias_worker.py',
                     'tools/check_i4v_alias.py', 'tools/test_check_i4v_alias.py',
                     'tools/check_i4v_stack_concurrent.py', 'tools/test_check_i4v_stack_concurrent.py',
                     'tools/check_i4v_stack_boundary.py', 'tools/test_i4v_stack_boundary_check.py',
                     'tools/i4v_address_worker.py', 'tools/test_i4v_address_worker.py',
                     'tools/check_i4v_address_concurrent.py', 'tools/test_check_i4v_address_concurrent.py',
                     'tools/i4v_fence_worker.py', 'tools/test_i4v_fence_worker.py',
                     'tools/check_i4v_fence.py', 'tools/test_check_i4v_fence.py',
                     'tools/check_i4v_fence_gate.py', 'tools/test_check_i4v_fence_gate.py',
                     'tools/testdata/i4v_fence_gate.json',
                     'tools/i4v_ordering_worker.py', 'tools/test_i4v_ordering_worker.py',
                     'tools/i4v_ordering_model.py', 'tools/test_i4v_ordering_model.py',
                     'tools/check_i4v_ordering.py', 'tools/test_check_i4v_ordering.py',
                     'tools/i4v_coherence_worker.py', 'tools/test_i4v_coherence_worker.py',
                     'tools/i4v_coherence_model.py', 'tools/test_i4v_coherence_model.py',
                     'tools/check_i4v_coherence.py', 'tools/test_check_i4v_coherence.py',
                     'tools/check_i4v_schedule.py', 'tools/test_check_i4v_schedule.py',
                     'tools/check_i4v_progress_protocols.py', 'tools/test_check_i4v_progress_protocols.py',
                     'tools/test_run_i1.py',
                     'tools/check_i4v_partial_progress.py', 'tools/test_check_i4v_partial_progress.py',
                     'tools/testdata/i4v_partial_n2.json.gz', 'tools/testdata/i4v_partial_n2.json.provenance.json',
                     'tools/i4v_atomic_schema.py',
                     'tools/check_i4v_containment.py', 'tools/test_check_i4v_containment.py',
                     'tools/testdata/i4v_containment_panic_n1.json.gz', 'tools/testdata/i4v_containment_panic_n1.json.provenance.json',
                     'tools/test_containment_supervisor.py',
                     'tools/test_supervisor_ownership.py', 'tools/test_run_i4v.py',
                     'tools/check_i4v_atomic_progress.py', 'tools/test_check_i4v_atomic_progress.py',
                     'tools/testdata/i4v_atomic_n4_seed2.json.gz', 'tools/testdata/i4v_atomic_n4_seed2.json.provenance.json',
                     'tools/testdata/i4v_coherence_parent_n1.json.gz',
                     'tools/testdata/i4v_coherence_parent_n1.json.provenance.json',
                     'tools/testdata/i4v_coherence_size_pae_n4.json.gz',
                     'tools/testdata/i4v_coherence_size_pae_n4.json.provenance.json',
                     'tools/testdata/i4v_coherence_ad_four_n3.json.gz',
                     'tools/testdata/i4v_coherence_ad_four_n3.json.provenance.json',
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

        def job(label, argv, oracle=False, wall=120, rss=512*1024**2, expected_exit=0, gate_file=None):
            directory = run / label
            directory.mkdir()
            specification = dict(argv=list(map(str, argv)), cwd=str(source), environment=environment)
            specification['expected_exit']=expected_exit
            if oracle:
                if oracle == 'progress':
                    specification['oracle_json_lines'] = True
                specification['oracle'] = str(source / ('tools/check_i4v_schedule.py' if oracle == 'progress'
                    else 'tools/check_i4v_coherence.py' if oracle == 'coherence'
                    else 'tools/check_i4v_fence.py' if oracle == 'fence'
                    else 'tools/check_i4v_ordering.py' if oracle == 'ordering'
                    else 'tools/check_i4v_address_concurrent.py' if oracle == 'address'
                    else 'tools/check_i4v_stack_boundary.py' if oracle == 'boundary'
                    else 'tools/i4v_controls_concurrent_check.py' if oracle == 'concurrent-controls'
                    else 'tools/i4v_controls_check.py' if oracle == 'controls'
                    else 'tools/check_i4v_mixed.py' if oracle == 'mixed'
                    else 'tools/i4v_off_steps_check.py' if oracle == 'off-steps'
                    else 'tools/i4v_off_check.py' if oracle == 'off32'
                    else 'tools/check_i4v_alias.py' if oracle == 'aliases'
                    else 'tools/check_i4v_stack_concurrent.py' if oracle == 'concurrent-stack' else 'tools/i4v_check.py'))
            path = directory / 'job.json'
            path.write_text(json.dumps(specification, indent=2)+'\n')
            remaining = min(wall, deadline - time.monotonic())
            if remaining <= 0:
                raise RuntimeError('I4-V execution bundle bound reached')
            result = supervisor.supervise(
                [sys.executable, '-B', str(source / 'tools/run_i1.py'), '--worker', str(path)],
                directory, wall=remaining, rss_limit=rss, period=.02 if gate_file else .05, gate_file=gate_file)
            (directory / 'supervision.json').write_text(json.dumps(result, indent=2)+'\n')
            record['jobs'].append(dict(name=label, **result))
            retention.save(run, record)
            if result['status'] != 'passed':
                raise RuntimeError(f'{label}: {result["status"]}; inspect retained output')
            if sum(p.stat().st_size for p in run.rglob('*') if p.is_file()) > record['limits']['run_disk_bytes']:
                raise RuntimeError('I4-V retained run disk bound reached')
            return directory

        job('rust-tests', ['cargo', '+1.97.0', 'test', '--offline', '--locked'], wall=600, rss=8*1024**3)
        job('runner-tests', [sys.executable,'-B','-m','unittest','discover','-s','tools','-p','test_run_i1.py'])
        job('i4v-runner-tests', [sys.executable,'-B','-m','unittest','discover','-s','tools','-p','test_run_i4v.py'])
        job('progress-protocol-tests', [sys.executable,'-B','-m','unittest','discover','-s','tools','-p','test_check_i4v_progress_protocols.py'])
        job('atomic-progress-tests', [sys.executable,'-B','-m','unittest','discover','-s','tools','-p','test_check_i4v_atomic_progress.py'],wall=180)
        job('containment-supervisor-tests',[sys.executable,'-B','-m','unittest','discover','-s','tools','-p','test_containment_supervisor.py'])
        job('supervisor-ownership-tests',[sys.executable,'-B','-m','unittest','discover','-s','tools','-p','test_supervisor_ownership.py'])
        job('containment-oracle-tests',[sys.executable,'-B','-m','unittest','discover','-s','tools','-p','test_check_i4v_containment.py'])
        for label, folder, pattern in (('producer-tests', 'tools/i4v_producer', 'test_*.py'),
                                       ('expectation-tests', 'fixtures/i4v', 'test_*.py'),
                                       ('oracle-tests', 'tools', 'test_i4v_check.py'),
                                       ('stack-oracle-tests', 'tools', 'test_i4v_stack_check.py'),
                                       ('stack-worker-tests', 'tools', 'test_i4v_stack_worker.py'),
                                       ('alias-oracle-tests', 'tools', 'test_check_i4v_alias.py'),
                                       ('alias-worker-tests', 'tools', 'test_i4v_alias_worker.py')):
            job(label, [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', folder, '-p', pattern])
        job('concurrent-stack-oracle-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_check_i4v_stack_concurrent.py'])
        job('stack-boundary-oracle-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_i4v_stack_boundary_check.py'])
        job('off32-worker-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_i4v_off_worker.py'])
        job('off32-oracle-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_i4v_off_check.py'])
        job('off-steps-worker-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_i4v_off_steps_worker.py'])
        job('off-steps-oracle-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_i4v_off_steps_check.py'])
        job('mixed-worker-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_i4v_mixed32_worker.py'])
        job('mixed-oracle-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_check_i4v_mixed.py'])
        job('controls-worker-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_i4v_controls_worker.py'])
        job('controls-oracle-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_i4v_controls_check.py'])
        job('concurrent-controls-oracle-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_i4v_controls_concurrent_check.py'])
        job('address-worker-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_i4v_address_worker.py'])
        job('address-oracle-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_check_i4v_address_concurrent.py'])
        for name in ('i4v_ordering_worker', 'i4v_ordering_model', 'check_i4v_ordering', 'i4v_fence_worker', 'check_i4v_fence', 'check_i4v_fence_gate', 'i4v_coherence_worker', 'i4v_coherence_model', 'check_i4v_coherence', 'check_i4v_schedule', 'check_i4v_partial_progress'):
            job(name+'-tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_'+name+'.py'])
        job('build-host', ['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release', '-p', 'natemu-host'], wall=600, rss=8*1024**3)
        job('build-runtime', ['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release', '-p', 'natemu-runtime', '--target', 'aarch64-unknown-none', '--features', 'families'], wall=600, rss=8*1024**3)
        host = source / 'target/release/natemu-host'
        runtime = source / 'target/aarch64-unknown-none/release/natemu-runtime'
        gate_job=job('fence-gate-audit', [sys.executable, '-B', 'tools/check_i4v_fence_gate.py', runtime]+(['--partial'] if args.progress or args.containment else []))
        fence_gates=json.loads((gate_job/'command.stdout').read_text())
        job('sign', ['codesign', '--force', '--sign', '-', '--entitlements', 'crates/host/entitlements.plist', host])
        job('verify-signature', ['codesign', '--verify', '--verbose=2', host])
        if args.containment:
            sys.path.insert(0,str(source/'tools'))
            containment=module(source/'tools/check_i4v_containment.py','containment_checker')
            scheduling=module(source/'tools/check_i4v_schedule.py','scheduling')
            for failure,n,actor in containment_cells:
                label=f'containment-f{failure}-n{n}-actor{actor}'
                if args.focus and label!=args.focus:continue
                output=run/(label+'-producer')
                result=job(label,[host,runtime,'i4v-containment',producer,failure,n,actor,source/'tools/i4v_controls_worker.py',output],
                    expected_exit=1,gate_file=output/'containment-gate.json')
                reports=[json.loads(line) for line in (result/'command.stdout').read_text().splitlines()]
                cfg=containment.configuration(failure,n,actor)
                containment.check(reports,cfg)
                bind_partial_store(reports[2],fence_gates['partial_store_pc'])
                for directory,exported in containment.verify_artifacts(reports,output):
                    record['compilations'].append(dict(directory=str(directory.relative_to(run)),export=exported))
                # A separate host process resets process-local cancellation state.
                fresh_output=run/(label+'-fresh-producer')
                fresh_cfg=containment.scheduler(cfg)
                family='partial' if cfg['family']==4 else 'controls'
                fresh=job(label+'-fresh',[host,runtime,'i4v-progress',producer,family,n,fresh_cfg['seed_ordinal'],
                    source/'tools/i4v_controls_worker.py',fresh_output],oracle='progress')
                fresh_reports=[json.loads(line) for line in (fresh/'command.stdout').read_text().splitlines()]
                bind_partial_store(fresh_reports[1],fence_gates['partial_store_pc'])
                containment.verify_fresh(reports,fresh_reports,fresh_output)
                for directory,exported in scheduling.verify_producer(fresh_reports,fresh_output):
                    record['compilations'].append(dict(directory=str(directory.relative_to(run)),export=exported))
                record['cells'].append(dict(name=label,producer=str(output.relative_to(run)),fresh_producer=str(fresh_output.relative_to(run)),
                    vcpus=n,failure=failure,actor=actor))
        if args.progress:
            sys.path.insert(0,str(source/'tools'))
            scheduling = module(source/'tools/check_i4v_schedule.py','scheduling')
            for family,n,ordinal in progress_cells:
                label=f'progress-{family}-n{n}-seed{ordinal}'
                if args.focus and label != args.focus:
                    continue
                output=run/(label+'-producer')
                worker={'stack':'i4v_stack_worker.py','controls':'i4v_controls_worker.py','coherence':'i4v_coherence_worker.py','atomic':'i4v_ordering_worker.py','partial':'i4v_coherence_worker.py'}[family]
                result=job(label,[host,runtime,'i4v-progress',producer,family,n,ordinal,source/'tools'/worker,output],oracle='progress')
                reports=[json.loads(line) for line in (result/'command.stdout').read_text().splitlines() if line.strip()]
                cfg=scheduling.configuration(('stack','controls','coherence','atomic','partial').index(family),n,ordinal)
                scheduling.check(reports,cfg)
                if family=='partial':
                    bind_partial_store(reports[1],fence_gates['partial_store_pc'])
                for directory,exported in scheduling.verify_producer(reports,output):
                    record['compilations'].append(dict(directory=str(directory.relative_to(run)),export=exported))
                record['cells'].append(dict(name=label,producer=str(output.relative_to(run)),vcpus=n,family=family,seed_ordinal=ordinal,seed=cfg['seed'],actor=cfg['actor']))
        if args.coherence:
            sys.path.insert(0, str(source / 'tools'))
            coherence_worker = module(source / 'tools/i4v_coherence_worker.py', 'coherence_worker')
            sys.path.pop(0)
            for profile, name, n, actor in coherence_cells:
                label = f'coherence-{name}-n{n}-actor{actor}'
                if args.focus and label != args.focus:
                    continue
                output = run / (label + '-producer')
                result = job(label, [host, runtime, 'i4v-coherence', producer, profile, n, actor,
                             source / 'tools/i4v_coherence_worker.py', output], oracle='coherence')
                report = json.loads((result / 'command.stdout').read_text())
                if (report['case'], report['profile'], report['n'], report['actor']) != ('coherence', profile, n, actor):
                    raise ValueError('coherence report differs from requested configuration')
                for owner in report['owners']:
                    directory = output / f"cpu-{owner['id']}"
                    if owner['compiler_requests'] != owner['requests']:
                        raise ValueError('coherence compiler and resident requests differ')
                    exports = coherence_worker.verify_compilations(directory, owner['requests'], owner['replies'])
                    for kind, exported in enumerate(exports):
                        record['compilations'].append(dict(directory=str((directory / f'request-{kind:03}').relative_to(run)), export=exported))
                record['cells'].append(dict(name=label, producer=str(output.relative_to(run)), profile=profile, vcpus=n, actor=actor))
        if args.fence:
            sys.path.insert(0,str(source/'tools'))
            fence_worker=module(source/'tools/i4v_fence_worker.py','fence_worker')
            sys.path.pop(0)
            for case in range(5):
                for n in range(1,5):
                    label=f'fence-case{case}-n{n}'
                    if args.focus and label!=args.focus:continue
                    output=run/(label+'-producer')
                    result=job(label,[host,runtime,'i4v-fence',producer,case,n,source/'tools/i4v_fence_worker.py',output],oracle='fence')
                    report=json.loads((result/'command.stdout').read_text())
                    if (report['case'],report['profile'],report['n'])!=('fence',case,n):raise ValueError('fence report differs from requested identity')
                    for owner in report['owners']:
                        if (owner['sink_pc'],owner['return_pc'],owner['capture_pc'])!=(fence_gates['sink_pc'],fence_gates['return_pc'],fence_gates['return_pc']):raise ValueError('fence report gates differ from producing ELF')
                        directory=output/f"cpu-{owner['id']}"
                        if owner['compiler_requests']!=owner['requests']:raise ValueError('fence request/delivery identities differ')
                        exported=fence_worker.verify_compilations(directory,owner['requests'],owner['replies'])
                        record['compilations'].append(dict(directory=str((directory/'request-000').relative_to(run)),export=exported))
                    record['cells'].append(dict(name=label,producer=str(output.relative_to(run)),profile=case,vcpus=n))
        if args.ordering:
            sys.path.insert(0,str(source/'tools'))
            ordering_worker=module(source/'tools/i4v_ordering_worker.py','ordering_worker')
            sys.path.pop(0)
            for profile,name,n,rotation,seed in ordering_cells:
                label=f'ordering-{name}-n{n}-rotation{rotation}-seed{seed}'
                if args.focus and label!=args.focus: continue
                output=run/(label+'-producer')
                result=job(label,[host,runtime,'i4v-ordering',producer,profile,n,rotation,seed,source/'tools/i4v_ordering_worker.py',output],oracle='ordering')
                report=json.loads((result/'command.stdout').read_text())
                if (report['case'],report['profile'],report['n'],report['rotation'],report['seed'])!=('ordering',profile,n,rotation,seed):
                    raise ValueError('ordering report differs from declared configuration')
                for owner in report['owners']:
                    directory=output/f"cpu-{owner['id']}"
                    requests=owner['compiler_requests']
                    if requests != [owner['request']]:raise ValueError('ordering requests differ from resident record')
                    ordering_worker.verify_compilations(directory,requests,[owner['reply']])
                    exported=ordering_worker.verify_request(directory/'request-000',requests[0])
                    record['compilations'].append(dict(directory=str((directory/'request-000').relative_to(run)),export=exported))
                record['cells'].append(dict(name=label,producer=str(output.relative_to(run)),profile=profile,vcpus=n,rotation=rotation,seed=seed))
        if args.address_forms:
            sys.path.insert(0,str(source/'tools'))
            address_worker = module(source/'tools/i4v_address_worker.py','address_worker')
            sys.path.pop(0)
            for profile,name in address_profiles:
                for n in (1,2,3,4):
                    for actor in range(n):
                        label=f'address-{name}-n{n}-actor{actor}'
                        if args.focus and label != args.focus:
                            continue
                        output=run/(label+'-producer')
                        result=job(label,[host,runtime,'i4v-address-concurrent',producer,profile,n,actor,source/'tools/i4v_address_worker.py',output],oracle='address')
                        report=json.loads((result/'command.stdout').read_text())
                        if (report['case'],report['profile'],report['configured_vcpus'],report['tested_vcpus'],report['actor']) != ('address-concurrent',profile,n,n,actor):
                            raise ValueError('address report differs from requested profile/owners')
                        for owner in report['owners']:
                            directory=output/f"cpu-{owner['id']}"
                            requests=owner['compiler_requests']
                            pairs=[(v['request'],v['reply']) for v in owner['visits'] if v['request'][0]]
                            pairs.append((owner['owner']['deferred'],owner['owner']['stale_reply']))
                            pairs.sort(key=lambda pair:pair[0][13])
                            if [p[0] for p in pairs] != requests:
                                raise ValueError('address compiler requests differ from resident deliveries')
                            address_worker.verify_compilations(directory,requests,[p[1] for p in pairs])
                            for index,request in enumerate(requests):
                                item=directory/f'request-{index:03}'
                                exported=address_worker.verify_request(item,request)
                                record['compilations'].append(dict(directory=str(item.relative_to(run)),export=exported))
                        record['cells'].append(dict(name=label,producer=str(output.relative_to(run)),vcpus=n,actor=actor,profile=profile))
        if args.stack_boundaries:
            sys.path.insert(0, str(source / 'tools'))
            boundary_worker = module(source / 'tools/i4v_stack_worker.py', 'boundary_stack_worker')
            sys.path.pop(0)
            for profile, outcome, n, actor, region in boundary_cases:
                label = f'boundary-{outcome}-n{n}-actor{actor}-{region}'
                if args.focus and label != args.focus:
                    continue
                output = run / (label+'-producer')
                result = job(label, [host,runtime,'i4v-stack-boundary',outcome,n,producer,output,actor,region], oracle='boundary')
                report = json.loads((result/'command.stdout').read_text())
                identity = (report['case'],report['profile'],report['outcome'],report['region'],report['configured_vcpus'],report['tested_vcpus'],report['actor'])
                if identity != ('stack-boundary',profile,outcome,region,n,n,actor):
                    raise ValueError('stack boundary report differs from requested outcome/owners')
                prefix = report['prefix']
                rip = 0x102000 + (0xffff800000000000 if region == 'high' else 0)
                if (prefix['profile'],prefix['alias_profile'],prefix['stack_profile'],prefix['source_rip'],prefix['configured_vcpus'],prefix['tested_vcpus'],prefix['delayed_owner_ms']) != ('i4-stack-'+region, 2 if region == 'high' else 1,1,rip,n,n,50):
                    raise ValueError('stack boundary pressure prefix differs from requested profile')
                boundary_worker.verify_compilations(output, prefix['requests'])
                compiler_owner = next(owner for owner in prefix['stacks'] if owner['id'] == 0)
                for index, request in enumerate(prefix['requests']):
                    directory = output / f'request-{index:03}'
                    exported = boundary_worker.verify_request(directory, request)
                    boundary_worker.verify_transfers(directory,
                        boundary_worker.requests_for(tuple(request)), compiler_owner['replies'][index])
                    if exported['guest_rip'] != rip:
                        raise ValueError('stack boundary compilation uses wrong full RIP')
                    record['compilations'].append(dict(directory=str(directory.relative_to(run)),export=exported))
                record['cells'].append(dict(name=label,producer=str(output.relative_to(run)),vcpus=n,profile=profile,actor=actor,region=region))
        # Exercise new control composition before the earlier regression families.
        if args.concurrent_controls:
            sys.path.insert(0,str(source/'tools'))
            controls_worker = module(source/'tools/i4v_controls_worker.py','controls_worker')
            sys.path.pop(0)
            for profile, scenario in concurrent_profiles:
                for n in (1,2,3,4):
                    for actor in range(n):
                        label=f'controls-concurrent-{scenario}-n{n}-actor{actor}'
                        if args.focus and label != args.focus:
                            continue
                        output=run/(label+'-producer')
                        result=job(label,[host,runtime,'i4v-controls-concurrent',producer,profile,n,actor,source/'tools/i4v_controls_worker.py',output],oracle='concurrent-controls')
                        report=json.loads((result/'command.stdout').read_text())
                        if (report['case'],report['program_revision'],report['profile'],report['configured_vcpus'],report['tested_vcpus'],report['actor']) != ('controls-concurrent',1,profile,n,n,actor):
                            raise ValueError('concurrent control report differs from requested program/owners')
                        for owner in report['owners']:
                            directory=output/f"cpu-{owner['id']}"
                            requests=owner['compiler_requests']
                            pairs=[(v['request'],v['reply']) for v in owner['visits'] if v['entries']]
                            if profile in (0,21,24):
                                pairs.append((owner['controls']['deferred'],owner['controls']['late_reply']))
                            if owner['owner']['source_rejections']:
                                pairs.append((owner['owner']['source_expected'],owner['owner']['source_reply']))
                            pairs.sort(key=lambda pair:pair[0][12])
                            if [p[0] for p in pairs] != requests or len(requests) != owner['requests']:
                                raise ValueError('concurrent compiler requests differ from resident deliveries')
                            controls_worker.verify_compilations(directory,requests,[p[1] for p in pairs])
                            for index,request in enumerate(requests):
                                item=directory/f'request-{index:03}'
                                exported=controls_worker.verify_request(item,request)
                                record['compilations'].append(dict(directory=str(item.relative_to(run)),export=exported))
                        record['cells'].append(dict(name=label,producer=str(output.relative_to(run)),vcpus=n,actor=actor,profile=profile))
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
        # Exact registries survive interrupted compiler logs. Cleanup never
        # signals a PID inferred from stale metadata or enumerates global jobs.
        cleanup_deadline=time.monotonic()+10
        registries=[path for path in sorted(run.rglob('.ownership/job.json')) if source not in path.parents]
        ownership=None
        if registries:
            try:
                ownership=module(source/'tools/i4v_producer/ownership.py','owned_cleanup')
            except Exception as error:
                record.setdefault('owned_cleanup',[]).append(dict(error='cleanup helper unavailable: '+str(error)))
                record['status']='failed'
        for path in registries if ownership is not None else []:
            left=max(0,cleanup_deadline-time.monotonic())
            try:
                if ownership.job(path.parent).get('kind')=='standalone-container':
                    root=path.parent.parent
                    label=record['standalone_compilers'].get(str(root.relative_to(run)))
                    receipt=next((j for j in record['jobs'] if j['name']==label),None)
                    if receipt is None:
                        cleanup=ownership.cleanup_containers(path.parent,budget=left)
                        cleanup.update(complete=False,error='standalone compiler has no explicit supervised-job binding')
                    else:
                        cleanup=ownership.cleanup_standalone(path.parent,expected_root=root,supervision=receipt,budget=left)
                else:
                    cleanup=ownership.cleanup_job(path.parent,budget=left)
                record.setdefault('owned_cleanup',[]).append(dict(registry=str(path.parent.relative_to(run)),report=cleanup))
                if not cleanup['complete']:
                    record['status']='failed'
            except Exception as error:
                record.setdefault('owned_cleanup',[]).append(dict(registry=str(path.parent.relative_to(run)),error=str(error)))
                record['status']='failed'
        record['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        retention.save(run, record)
        retention.checksums(run)
    if record['status'] != 'passed':
        raise RuntimeError('I4-V cleanup failed; see retained manifest')
    print(f'PASS {len(record["cells"])} native address cells; full I4-V remains incomplete')


if __name__ == '__main__':
    main()

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from i4v_producer import ownership as o


class FakeDocker:
    def __init__(self, token, name, *, foreign=False, failure=False):
        self.token=token;self.name=name;self.identity='a'*64;self.exists=True;self.foreign=foreign;self.failure=failure;self.calls=[]
    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[:2]==['docker','inspect']:
            if not self.exists:return subprocess.CompletedProcess(argv,1,'','Error: No such object: '+self.name)
            value=dict(Id=self.identity,Name='/'+self.name,Config=dict(Labels={'natemu.job':'other' if self.foreign else self.token}))
            return subprocess.CompletedProcess(argv,0,json.dumps([value]),'')
        if argv[:2]==['docker','rm']:
            if self.failure:raise subprocess.TimeoutExpired(argv,kwargs['timeout'])
            self.exists=False;return subprocess.CompletedProcess(argv,0,self.name,'')
        raise AssertionError(argv)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        self.owned=o.Container(self.directory,'natemu-test-owned')
        self.registry=self.owned.registry
    def test_registration_precedes_creation_and_is_independent_of_partial_compiler_record(self):
        self.assertEqual(o.read_json(self.owned.path)['state'],'registered')
        self.owned.creating();self.owned.created('a'*64)
        (self.directory/'compiler.json').write_text('{broken')
        docker=FakeDocker(self.owned.record['token'],self.owned.record['name'])
        report=o.cleanup_containers(self.registry,run=docker)
        self.assertTrue(report['complete']);self.assertFalse(docker.exists)
        self.assertEqual([c[1] for c in docker.calls],['inspect','rm','inspect'])
        self.assertEqual([c[-1] for c in docker.calls],['natemu-test-owned','a'*64,'a'*64])
    def test_container_hold_publishes_only_acknowledged_identity(self):
        (self.registry/'hold-container').touch()
        self.owned.creating()
        self.assertFalse((self.registry/'container-ready.bin').exists())
        with self.assertRaises(ValueError):self.owned.created('not-an-id')
        self.assertFalse((self.registry/'container-ready.bin').exists())
        self.owned.created('a'*64)
        marker=(self.registry/'container-ready.bin').read_bytes()
        self.assertEqual(marker,b'NATCTR01'+bytes.fromhex(self.owned.record['token'])+bytes.fromhex('a'*64)+bytes(8))
        self.assertEqual(o.read_json(self.registry/'creation-held.json'),self.owned.record)
        calls=[]
        def release(delay):
            calls.append(delay)
            (self.registry/'hold-container').unlink()
        with patch.object(o.time,'sleep',side_effect=release):self.owned.wait_before_start()
        self.assertEqual(calls,[.01])
    def test_cancelled_container_hold_never_reaches_start_and_removes_exact_id(self):
        (self.registry/'hold-container').touch();self.owned.created('a'*64)
        def cancel(delay):(self.registry/'cancelled').touch()
        with patch.object(o.time,'sleep',side_effect=cancel),self.assertRaisesRegex(RuntimeError,'cancelled'):
            self.owned.wait_before_start()
        docker=FakeDocker(self.owned.record['token'],self.owned.record['name'])
        self.assertTrue(o.cleanup_containers(self.registry,run=docker)['complete'])
        self.assertEqual(docker.calls[1],['docker','rm','--force','--volumes','a'*64])
    def test_ordinary_container_has_no_hold_witness(self):
        self.owned.created('a'*64);self.owned.wait_before_start()
        self.assertFalse((self.registry/'container-ready.bin').exists())
        self.assertFalse((self.registry/'creation-held.json').exists())
    def test_name_replacement_after_inspection_never_removes_foreign_container(self):
        self.owned.created('a'*64)
        names={'natemu-test-owned':'a'*64};alive={'a'*64,'b'*64};calls=[]
        def daemon(argv,**kwargs):
            calls.append(argv)
            if argv[1]=='inspect':
                identity=names.get(argv[-1],argv[-1])
                if identity not in alive:return subprocess.CompletedProcess(argv,1,'','No such object: '+argv[-1])
                value=dict(Id=identity,Name='/natemu-test-owned',Config=dict(Labels={'natemu.job':self.owned.record['token']}))
                # A foreign container takes the name after our observation.
                names['natemu-test-owned']='b'*64
                return subprocess.CompletedProcess(argv,0,json.dumps([value]),'')
            if argv[1]=='rm':
                identity=names.get(argv[-1],argv[-1]);alive.remove(identity)
                return subprocess.CompletedProcess(argv,0,identity,'')
            self.fail(argv)
        self.assertTrue(o.cleanup_containers(self.registry,run=daemon)['complete'])
        self.assertEqual(alive,{'b'*64})
        self.assertEqual(calls[1][-1],'a'*64)
    def test_creation_identity_mismatch_prevents_removal_even_with_same_label(self):
        self.owned.created('b'*64)
        docker=FakeDocker(self.owned.record['token'],self.owned.record['name'])
        self.assertFalse(o.cleanup_containers(self.registry,run=docker)['complete'])
        self.assertEqual([c[1] for c in docker.calls],['inspect'])
    def test_foreign_label_and_unknown_registry_never_remove_any_container(self):
        self.owned.creating();self.owned.created('a'*64)
        docker=FakeDocker(self.owned.record['token'],self.owned.record['name'],foreign=True)
        self.assertFalse(o.cleanup_containers(self.registry,run=docker)['complete'])
        self.assertEqual([c[1] for c in docker.calls],['inspect'])
        bad=o.read_json(self.owned.path);bad['token']='0'*32;o.atomic_json(self.owned.path,bad)
        docker.calls=[]
        self.assertFalse(o.cleanup_containers(self.registry,run=docker)['complete'])
        self.assertEqual(docker.calls,[])
    def test_create_interrupt_is_not_called_clean_just_because_container_is_absent(self):
        self.owned.creating()
        docker=FakeDocker(self.owned.record['token'],self.owned.record['name']);docker.exists=False
        result=o.cleanup_containers(self.registry,run=docker)
        self.assertFalse(result['complete'])
        self.assertIn('unacknowledged',result['containers'][0]['error'])
    def test_lowercase_absence_acknowledges_removal_but_not_unacknowledged_creation(self):
        self.owned.creating();self.owned.created('a'*64)
        docker=FakeDocker(self.owned.record['token'],self.owned.record['name'])
        def lowercase(argv,**kwargs):
            result=docker(argv,**kwargs)
            result.stderr=result.stderr.lower()
            return result
        self.assertTrue(o.cleanup_containers(self.registry,run=lowercase)['complete'])
        self.assertEqual([c[-1] for c in docker.calls],['natemu-test-owned','a'*64,'a'*64])
        self.owned.creating()
        self.assertFalse(o.cleanup_containers(self.registry,run=lowercase)['complete'])
    def test_inspect_daemon_failure_is_not_absence(self):
        self.owned.created('a'*64)
        def unavailable(argv,**kwargs):
            return subprocess.CompletedProcess(argv,1,'','Cannot connect to the Docker daemon')
        report=o.cleanup_containers(self.registry,run=unavailable)
        self.assertFalse(report['complete'])
        self.assertIn('inspection failed',report['containers'][0]['error'])
    def test_removal_timeout_is_bounded_and_retains_owned_intent(self):
        self.owned.created('a'*64)
        docker=FakeDocker(self.owned.record['token'],self.owned.record['name'],failure=True)
        result=o.cleanup_containers(self.registry,run=docker,budget=.1)
        self.assertFalse(result['complete']);self.assertTrue(docker.exists)
        self.assertEqual(o.read_json(self.owned.path)['state'],'created')
    def test_normal_removed_ack_does_not_execute_new_compiler_or_docker_commands(self):
        self.owned.removed()
        def forbidden(*args,**kwargs):raise AssertionError('unexpected subprocess')
        self.assertTrue(o.cleanup_containers(self.registry,run=forbidden)['complete'])
    def test_cancellation_prevents_new_container_registration(self):
        (self.registry/'cancelled').touch()
        with patch.dict(os.environ,{o.ENV:str(self.registry)}),self.assertRaisesRegex(RuntimeError,'cancelled'):
            o.Container(self.directory,'natemu-test-new')
        self.assertEqual(len(list((self.registry/'containers').glob('*.json'))),1)
    def test_atomic_update_never_truncates_previous_record_on_replace_failure(self):
        old=o.read_json(self.owned.path)
        with patch.object(o.os,'replace',side_effect=OSError('injected')),self.assertRaises(OSError):
            o.atomic_json(self.owned.path,dict(state='bad'))
        self.assertEqual(o.read_json(self.owned.path),old)
        self.assertFalse(list(self.registry.rglob('*.tmp-*')))


class CompilerCleanupTests(unittest.TestCase):
    def test_normal_finally_removes_created_id_even_after_name_is_rebound(self):
        from i4v_producer import compile as compiler
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);producer=root/'producer';producer.write_bytes(b'never executed')
            expected=json.loads(Path(compiler.__file__).with_name('producer.json').read_text())['binary_sha256']
            class Digest:
                def hexdigest(self):return expected
            alive={'c'*64,'f'*64};bound_name={};removed=[]
            def fake_run(argv,**kwargs):
                if argv[1]=='create':
                    bound_name[argv[argv.index('--name')+1]]='c'*64
                    return subprocess.CompletedProcess(argv,0,'c'*64+'\n','')
                if argv[1]=='start':
                    bound_name[argv[-1]]='f'*64
                    raise subprocess.TimeoutExpired(argv,40)
                if argv[1]=='rm':
                    identity=bound_name.get(argv[-1],argv[-1]);removed.append(identity);alive.remove(identity)
                    return subprocess.CompletedProcess(argv,0,identity,'')
                self.fail(argv)
            with patch.object(compiler.hashlib,'sha256',return_value=Digest()),patch.object(compiler.subprocess,'run',side_effect=fake_run):
                with self.assertRaises(subprocess.TimeoutExpired):
                    compiler.compile_source(root/'compile',producer,b'\x8b\x03',0x102000,64,1,2)
            self.assertEqual(removed,['c'*64]);self.assertEqual(alive,{'f'*64})
            record=json.loads((root/'compile/compiler.json').read_text())
            self.assertIn('TimeoutExpired',record['error']);self.assertNotIn('cleanup_error',record)
    def test_primary_compile_timeout_survives_cleanup_failure_and_truncated_log(self):
        from i4v_producer import compile as compiler
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);producer=root/'producer';producer.write_bytes(b'fake producer, never executed')
            expected=json.loads(Path(compiler.__file__).with_name('producer.json').read_text())['binary_sha256']
            class Digest:
                def hexdigest(self):return expected
            directory=root/'compile'
            def fake_run(argv,**kwargs):
                records=list((directory/'.ownership/containers').glob('*.json'))
                self.assertEqual(len(records),1)
                registry=o.read_json(records[0])
                if argv[1]=='create':
                    self.assertEqual(registry['state'],'creating')
                    self.assertIn('natemu.job='+registry['token'],argv)
                    return subprocess.CompletedProcess(argv,0,'c'*64+'\n','')
                if argv[1]=='start':
                    self.assertEqual(registry['state'],'created')
                    raise subprocess.TimeoutExpired(argv,40)
                if argv[1]=='rm':raise RuntimeError('secondary cleanup failure')
                raise AssertionError(argv)
            with patch.object(compiler.hashlib,'sha256',return_value=Digest()),patch.object(compiler.subprocess,'run',side_effect=fake_run):
                with self.assertRaises(subprocess.TimeoutExpired):
                    compiler.compile_source(directory,producer,b'\x8b\x03',0x102000,64,1,2)
            record=json.loads((directory/'compiler.json').read_text())
            self.assertEqual(record['status'],'failed')
            self.assertIn('TimeoutExpired',record['error'])
            self.assertIn('secondary cleanup failure',record['cleanup_error'])
            self.assertEqual(o.read_json(next((directory/'.ownership/containers').glob('*.json')))['state'],'created')


class ProcessLeaseTests(unittest.TestCase):
    def start(self, source, *, held=False):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);registry=root/'.ownership';o.reserve(registry,kind=None)
        if held:(registry/'hold-payload').touch()
        worker=root/'worker.py';worker.write_text(source)
        child=subprocess.Popen([sys.executable,'-B',str(Path(o.__file__).resolve()),'worker','--registry',str(registry),'--',
                                'python3','-B',str(worker)],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        def stop():
            if child.poll() is None:
                os.killpg(child.pid,signal.SIGKILL)
            child.wait(timeout=3)
        self.addCleanup(stop)
        return root,registry,child
    def wait_file(self,path):
        until=time.monotonic()+4
        while time.monotonic()<until:
            if path.exists():return path.read_bytes()
            time.sleep(.01)
        self.fail('worker observation deadline: '+str(path))
    def test_registered_leader_stays_alive_after_success_until_owner_releases_it(self):
        root,registry,child=self.start('pass\n')
        self.assertEqual(self.wait_file(registry/'result.bin'),bytes(4))
        self.assertIsNone(child.poll())
        metadata=o.read_json(registry/'worker.json')
        self.assertEqual((metadata['pid'],metadata['pgid']),(child.pid,child.pid))
        self.assertEqual(metadata['token'],o.job(registry)['token'])
        os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=3)
        with self.assertRaises(ProcessLookupError):os.killpg(child.pid,0)
    def test_worker_failure_status_survives_lease_and_does_not_run_cleanup_as_a_new_compile(self):
        _,registry,child=self.start("raise RuntimeError('original injected failure')\n")
        self.assertEqual(int.from_bytes(self.wait_file(registry/'result.bin'),'little'),256)
        self.assertEqual(o.read_json(registry/'worker-result.json')['exit_code'],1)
        self.assertIsNone(child.poll())
    def test_payload_hold_is_registered_before_work_and_released_explicitly(self):
        _,registry,child=self.start('pass\n',held=True)
        self.wait_file(registry/'worker.json')
        self.assertIsNone(child.poll());self.assertFalse((registry/'result.bin').exists())
        (registry/'hold-payload').unlink()
        self.assertEqual(self.wait_file(registry/'result.bin'),bytes(4))
    def test_registration_marker_binds_live_held_pid_and_prior_json(self):
        _,registry,child=self.start("raise AssertionError('held payload')\n",held=True)
        marker=self.wait_file(registry/'worker-ready.bin')
        self.assertEqual(marker,b'NATWRK01'+bytes.fromhex(o.job(registry)['token'])+child.pid.to_bytes(8,'little'))
        metadata=o.read_json(registry/'worker.json')
        self.assertEqual(metadata['pid'],child.pid)
        self.assertEqual(metadata['token'],o.job(registry)['token'])
        self.assertIsNone(child.poll());self.assertFalse((registry/'result.bin').exists())
    def test_cancellation_during_payload_hold_never_executes_payload(self):
        _,registry,child=self.start("raise AssertionError('must not execute')\n",held=True)
        self.wait_file(registry/'worker.json');(registry/'cancelled').touch()
        self.assertNotEqual(child.wait(timeout=3),0)
        self.assertFalse((registry/'result.bin').exists())
    def test_process_group_contains_spawned_worker_descendants(self):
        source="""import os,subprocess,sys,time
from pathlib import Path
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
Path(os.environ['NATEMU_WORKER_REGISTRY']).joinpath('descendant').write_text(str(child.pid))
time.sleep(60)
"""
        _,registry,child=self.start(source)
        descendant=int(self.wait_file(registry/'descendant'))
        self.assertEqual(os.getpgid(descendant),child.pid)
        os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=3)
        until=time.monotonic()+3
        while time.monotonic()<until:
            try:os.killpg(child.pid,0)
            except ProcessLookupError:break
            time.sleep(.02)
        else:self.fail('owned descendant process group not drained')


class ExternalCleanupTests(unittest.TestCase):
    def test_no_journal_or_missing_leader_is_not_permission_to_signal_a_group(self):
        for registered in (False,True):
            with tempfile.TemporaryDirectory() as temporary:
                registry=Path(temporary)/'.ownership';j=o.reserve(registry,kind=None)
                if registered:o.atomic_json(registry/'worker.json',dict(schema=o.SCHEMA,token=j['token'],pid=999999,pgid=999999,started='old reused process'))
                with patch.object(o.os,'killpg',side_effect=AssertionError('unowned signal')):
                    result=o.cleanup_job(registry)
                self.assertFalse(result['complete']);self.assertFalse(result['process']['group_gone'])
    def test_only_acknowledged_process_drain_allows_external_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            registry=Path(temporary)/'.ownership';j=o.reserve(registry,kind=None)
            o.atomic_json(registry/'process.json',dict(schema=o.SCHEMA,token=j['token'],group_gone=True,reaped=True))
            with patch.object(o.os,'killpg',side_effect=AssertionError('unowned signal')):
                self.assertTrue(o.cleanup_job(registry)['complete'])
    def test_group_absence_does_not_substitute_for_reaping_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            registry=Path(temporary)/'.ownership';j=o.reserve(registry,kind=None)
            o.atomic_json(registry/'process.json',dict(schema=o.SCHEMA,token=j['token'],group_gone=True,reaped=False))
            result=o.cleanup_job(registry)
            self.assertFalse(result['complete']);self.assertIn('reaping',result['process']['error'])
    def test_acknowledged_spawn_failure_requires_no_reap(self):
        with tempfile.TemporaryDirectory() as temporary:
            registry=Path(temporary)/'.ownership';j=o.reserve(registry,kind=None)
            o.atomic_json(registry/'process.json',dict(schema=o.SCHEMA,token=j['token'],group_gone=True,reaped=False,spawned=False))
            self.assertTrue(o.cleanup_job(registry)['complete'])

class StandaloneCleanupTests(unittest.TestCase):
    def test_standalone_cleanup_is_explicitly_bound_and_never_claims_worker_reap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);owned=o.Container(root,'natemu-standalone-test')
            self.assertEqual(o.job(owned.registry)['kind'],'standalone-container')
            owned.created('a'*64)
            with self.assertRaisesRegex(ValueError,'explicitly bound'):o.cleanup_job(owned.registry)
            docker=FakeDocker(owned.record['token'],owned.record['name'])
            supervised=dict(status='passed',returncode=0,pid=123,process_reaped=True,group_signalled=True)
            cleanup=o.cleanup_containers
            with patch.object(o,'cleanup_containers',side_effect=lambda registry,**kw:cleanup(registry,run=docker,**kw)),patch.object(o.os,'killpg',side_effect=AssertionError('unowned signal')):
                report=o.cleanup_standalone(owned.registry,expected_root=root,supervision=supervised)
            self.assertTrue(report['complete']);self.assertFalse(docker.exists)
            self.assertEqual(report['scope'],'standalone-containers')
            self.assertNotIn('process',report)
            self.assertFalse((owned.registry/'process.json').exists())
            self.assertEqual(report['external_supervision'],supervised)
    def test_failed_job_still_cleans_owned_container_and_missing_reap_fails_closed(self):
        for status, reaped, complete in (('oracle_failed',True,True),('wall_limit',True,True),
                ('supervisor_error',False,False),('running',True,False),('passed',None,False)):
            with self.subTest(status=status,reaped=reaped),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);owned=o.Container(root,'natemu-standalone-test');owned.created('a'*64)
                docker=FakeDocker(owned.record['token'],owned.record['name']);cleanup=o.cleanup_containers
                supervision=dict(status=status,returncode=1,pid=123,process_reaped=reaped,group_signalled=True)
                with patch.object(o,'cleanup_containers',side_effect=lambda registry,**kw:cleanup(registry,run=docker,**kw)):
                    report=o.cleanup_standalone(owned.registry,expected_root=root,supervision=supervision)
                self.assertFalse(docker.exists)
                self.assertEqual(report['complete'],complete)
                self.assertEqual(report['external_supervision'],supervision)
    def test_reaped_leader_without_safe_group_cleanup_cannot_report_complete(self):
        for fields in ({},dict(group_signalled=1),dict(group_signalled=True,signal_error='live member'),
                dict(group_absent=True,lease_error='ECHILD'),dict(group_quiescent=True,group_observation_error='ps failed'),
                dict(group_signalled=True,reap_error='missing receipt')):
            with self.subTest(fields=fields),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);owned=o.Container(root,'natemu-standalone-test');owned.created('a'*64)
                docker=FakeDocker(owned.record['token'],owned.record['name']);cleanup=o.cleanup_containers
                supervision=dict(status='supervisor_error',returncode=0,pid=123,process_reaped=True,**fields)
                with patch.object(o,'cleanup_containers',side_effect=lambda registry,**kw:cleanup(registry,run=docker,**kw)):
                    report=o.cleanup_standalone(owned.registry,expected_root=root,supervision=supervision)
                self.assertFalse(docker.exists);self.assertFalse(report['complete'])
                self.assertEqual(report['external_supervision'],supervision)
    def test_absent_or_proven_quiescent_group_receipt_is_preserved(self):
        for fields in (dict(group_absent=True),dict(group_quiescent=True,
                quiescent_group_members=[dict(pid=123,pgid=123,state='Z')])):
            with self.subTest(fields=fields),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);registry=root/'.ownership';o.reserve(registry)
                supervision=dict(status='oracle_failed',returncode=1,pid=123,process_reaped=True,**fields)
                report=o.cleanup_standalone(registry,expected_root=root,supervision=supervision)
                self.assertTrue(report['complete'])
                self.assertEqual(report['external_supervision'],supervision)
    def test_missing_supervision_still_cleans_exact_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);owned=o.Container(root,'natemu-standalone-test');owned.created('a'*64)
            docker=FakeDocker(owned.record['token'],owned.record['name']);cleanup=o.cleanup_containers
            with patch.object(o,'cleanup_containers',side_effect=lambda registry,**kw:cleanup(registry,run=docker,**kw)):
                report=o.cleanup_standalone(owned.registry,expected_root=root,supervision={})
            self.assertFalse(docker.exists);self.assertFalse(report['complete'])
            self.assertIn('reaping',report['process_error'])
    def test_wrong_root_and_worker_records_fail_before_any_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);owned=o.Container(root,'natemu-standalone-test')
            good=dict(status='passed',returncode=0,pid=123,process_reaped=True)
            with patch.object(o,'cleanup_containers',side_effect=AssertionError('unbound cleanup')):
                with self.assertRaises(ValueError):o.cleanup_standalone(owned.registry,expected_root=root/'other',supervision=good)
                o.atomic_json(owned.registry/'worker.json',dict(pid=123))
                with self.assertRaisesRegex(ValueError,'lifecycle'):o.cleanup_standalone(owned.registry,expected_root=root,supervision=good)
    def test_host_worker_registry_cannot_be_exempted_by_passed_external_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);registry=root/'.ownership';o.reserve(registry,kind=None)
            with self.assertRaisesRegex(ValueError,'explicit standalone'):
                o.cleanup_standalone(registry,expected_root=root,supervision=dict(status='passed',returncode=0,pid=123))


class SnapshotImports(unittest.TestCase):
    def test_probe_snapshots_retain_ownership_dependency(self):
        import ast
        for path in Path(o.__file__).parent.glob('probe*.py'):
            tree=ast.parse(path.read_text())
            bundles=[n for n in ast.walk(tree) if isinstance(n,(ast.Tuple,ast.List))
                     and any(isinstance(x,ast.Constant) and x.value=='compile.py' for x in n.elts)]
            for bundle in bundles:
                self.assertTrue(any(isinstance(x,ast.Constant) and x.value=='ownership.py' for x in bundle.elts),str(path))
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for name in ('compile.py','ownership.py','export.py','producer.json'):
                (root/name).write_bytes(Path(o.__file__).with_name(name).read_bytes())
            result=subprocess.run([sys.executable,'-B','-c','import compile; import ownership; assert compile.Container is ownership.Container'],cwd=root,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)

if __name__=='__main__':unittest.main()

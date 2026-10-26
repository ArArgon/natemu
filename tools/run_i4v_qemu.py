#!/usr/bin/env python3
"""Supporting scalar QEMU comparison, isolated from the native campaign inputs."""
import argparse, hashlib, json, pathlib, shutil, subprocess, sys, time, uuid
sys.path.insert(0,str(pathlib.Path(__file__).parent/'i4v_qemu'))
from model import SOURCES, project, assembly, compare, require

IMAGE='sha256:27475cb39ecb4500d02b7c6e73c9d598e8d9f0736f0dc217d2d985e5cbe01aac'
PINS={
'qemu-arm64.deb':'e420c417c12b42ca774795b62888d5388da0e08c1ef263dea1490e5ce4e32a0b',
'qemu-x86_64-static':'16ce7dbb6626f3c0e1f932f831817dbf3b506c848a168d16f2172151e9666594',
'qemu.dsc':'6b6bd27b947ec89e40a1d6b26d9c9f3c698ca691da13163e0483f6061bbb20b3',
'debian.tar.xz':'28a1c86310ee53b6bf1854cffe6f6be6ed29a08db7d05759f8ac57b8d6a6cb2f',
'qemu-source.tar.xz':'91aca71520040edc40b8d437aa3004dae614f58e286cf653ee8996c07af2962f'}

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def write(path,value): path.write_text(json.dumps(value,indent=2,sort_keys=True)+'\n')
def checksums(root):
    (root/'SHA256SUMS').write_text(''.join(f'{sha(p)}  {p.relative_to(root)}\n' for p in sorted(root.rglob('*')) if p.is_file() and p.name!='SHA256SUMS'))
def resident(root, name, dest):
    root=root.resolve(); manifest=json.loads((root/'manifest.json').read_text())
    require(manifest['status']=='passed','resident bundle must be passed')
    jobs=[j for j in manifest['jobs'] if j['name']==name]
    require(len(jobs)==1 and jobs[0]['status']=='passed' and jobs[0]['returncode']==0,'resident selected job passed')
    path=root/name/'command.stdout'; expected={line.split('  ',1)[1]:line.split('  ',1)[0] for line in (root/'SHA256SUMS').read_text().splitlines()}
    require(expected[str(path.relative_to(root))]==sha(path),'resident report retained checksum')
    shutil.copy2(path,dest/'resident.json'); shutil.copy2(root/'manifest.json',dest/'resident-manifest.json')
    shutil.copy2(root/'SHA256SUMS',dest/'resident-SHA256SUMS')
    write(dest/'resident-binding.json',{'root':str(root),'job':name,'report_sha256':sha(path),'manifest_sha256':sha(root/'manifest.json')})
    return json.loads(path.read_text())

def prepare(args):
    out=args.output.resolve(); out.mkdir(parents=True,exist_ok=False)
    (out/'pin').mkdir(); (out/'cases').mkdir(); (out/'source').mkdir()
    for name,digest in PINS.items():
        source=args.acquisition/name; require(sha(source)==digest,'pinned '+name)
        shutil.copy2(source,out/'pin'/name)
    for name in ('copyright','changelog.Debian.gz','qemu-package.txt'):
        shutil.copy2(args.acquisition/name,out/'pin'/name)
    (out/'pin'/'qemu-x86_64-static').chmod(0o755)
    here=pathlib.Path(__file__).resolve()
    shutil.copy2(here,out/'source'/here.name)
    shutil.copy2(here.parent.parent/'fixtures/i4v-qemu/README.md',out/'source'/'README.md')
    for name in ('model.py','container.py','test_model.py'):
        shutil.copy2(here.parent/'i4v_qemu'/name,out/'source'/name)
    for case in ('load','stack','xchg','lfence','sfence','mfence'):
        d=out/'cases'/case; d.mkdir()
        if case=='load': root,name=args.load_root,'live-low-low_operand'
        elif case=='stack': root,name=args.stack_root,'stack-low-low_stack'
        else: root,name=args.ordering_root,f'ordering-{case}-n1-rotation0-seed1'
        p=project(case,resident(root,name,d)); write(d/'projection.json',p)
        (d/'memory.bin').write_bytes(bytes.fromhex(p['memory_before']))
        (d/'harness.asm').write_text(assembly(p))
    write(out/'manifest.json',{'status':'prepared','format':'i4v-qemu-scalar-v1','image':IMAGE,'pins':PINS,
        'scope':'Six sequential scalar projections; no paging, fault-retry, concurrency, fence-ordering or publication qualification.',
        'flags':'Require exact full RFLAGS 0x202 for this fixed Linux-user profile; no flags are masked. Load/stack canonical RFLAGS also equal 0x202. Ordering reports expose native NZCV preservation, not canonical RFLAGS equivalence.',
        'qemu_only':'Load memory beyond 256 bytes and ordering R8..R15 are extra QEMU canaries, not resident equivalence.',
        'source_package':'qemu 1:7.2+dfsg-7+deb12u18','binary_package':'qemu-user-static 1:7.2+dfsg-7+deb12u18+b3 arm64'})
    checksums(out); print(out)

def audit(out):
    from model import elf
    require({p.name for p in (out/'cases').iterdir()}==set(SOURCES),'exact six comparison cases')
    for name,digest in PINS.items(): require(sha(out/'pin'/name)==digest,'audit producing pin '+name)
    results=[]
    for d in sorted((out/'cases').iterdir()):
        binding=json.loads((d/'resident-binding.json').read_text())
        require(sha(d/'resident.json')==binding['report_sha256'],'audit resident report digest')
        require(sha(d/'resident-manifest.json')==binding['manifest_sha256'],'audit resident manifest digest')
        retained={line.split('  ',1)[1]:line.split('  ',1)[0] for line in (d/'resident-SHA256SUMS').read_text().splitlines()}
        require(retained[binding['job']+'/command.stdout']==binding['report_sha256'],'audit retained checksum binding')
        p=project(d.name,json.loads((d/'resident.json').read_text()))
        require(p==json.loads((d/'projection.json').read_text()),'projection regenerated from resident')
        require(assembly(p)==(d/'harness.asm').read_text(),'capture/setup source unchanged')
        require(elf((d/'harness.bin').read_bytes(),p)==(d/'harness.elf').read_bytes(),'audit exact producing ELF')
        command=json.loads((d/'qemu.command.json').read_text())
        require(command=={'argv':['/work/pin/qemu-x86_64-static','-cpu','qemu64','-d','in_asm','-D','qemu.log','harness.elf'],'returncode':0},'audit QEMU invocation')
        results.append(compare(p,(d/'qemu.stdout').read_bytes()))
    return results

def execute(out):
    out=out.resolve(); m=json.loads((out/'manifest.json').read_text());require(m['status']=='prepared','fresh prepared bundle only')
    for line in (out/'SHA256SUMS').read_text().splitlines():
        digest,name=line.split('  ',1); require(sha(out/name)==digest,'prepared input checksum '+name)
    m['status']='running'; write(out/'manifest.json',m)
    cid=None; started=time.monotonic(); cleanup=None
    token=uuid.uuid4().hex; name='natemu-qemu-'+token
    write(out/'container-owner.json',{'name':name,'token':token})
    try:
        inspect=subprocess.run(['docker','image','inspect',IMAGE],capture_output=True,check=True,timeout=20)
        (out/'image-inspect.json').write_bytes(inspect.stdout)
        require(json.loads(inspect.stdout)[0]['Id']==IMAGE,'pinned container image ID')
        args=['docker','create','--name',name,'--label','natemu.qemu.token='+token,'--cidfile',str(out/'container-id'),'--network','none','--read-only','--cpus','2','--memory','512m','--pids-limit','128',
              '--tmpfs','/tmp:rw,size=32m','--tmpfs','/var/lib/postgresql/data:rw,size=1m','--mount',f'type=bind,source={out},target=/work','--workdir','/work',
              '--entrypoint','python3',IMAGE,'/work/source/container.py']
        write(out/'container-command.json',args)
        created=subprocess.run(args,capture_output=True,check=True,timeout=20); cid=created.stdout.decode().strip()
        require(len(cid)==64 and all(c in '0123456789abcdef' for c in cid),'exact container ID')
        require((out/'container-id').read_text().strip()==cid,'Docker cidfile identity')
        result=subprocess.run(['docker','start','--attach',cid],capture_output=True,timeout=180)
        (out/'container.stdout').write_bytes(result.stdout); (out/'container.stderr').write_bytes(result.stderr)
        require(result.returncode==0,'container execution failed')
        results=audit(out);write(out/'comparison.json',results);m['status']='passed'
    except BaseException as error:
        m['status']='failed';m['error']=repr(error);raise
    finally:
        if cid is None and (out/'container-id').exists():
            candidate=(out/'container-id').read_text().strip()
            if len(candidate)==64 and all(c in '0123456789abcdef' for c in candidate): cid=candidate
        if cid is None:
            try:
                observed=subprocess.run(['docker','container','inspect',name],capture_output=True,timeout=20)
                (out/'cleanup-inspect.stdout').write_bytes(observed.stdout);(out/'cleanup-inspect.stderr').write_bytes(observed.stderr)
                if observed.returncode==0:
                    obj=json.loads(observed.stdout)[0]
                    require(obj['Config']['Labels'].get('natemu.qemu.token')==token,'reserved container ownership label')
                    cid=obj['Id']
                elif b'no such container' not in observed.stderr.lower() and b'no such object' not in observed.stderr.lower():
                    raise RuntimeError('cannot establish reserved container absence')
            except BaseException as error: m['status']='failed';m['cleanup_error']=repr(error)
        if cid:
            try:
                removed=subprocess.run(['docker','rm','--force','--volumes',cid],capture_output=True,timeout=20)
                cleanup={'id':cid,'returncode':removed.returncode,'stdout':removed.stdout.decode(),'stderr':removed.stderr.decode()}
                if removed.returncode: m['status']='failed';m['cleanup_error']='exact container removal failed'
            except BaseException as error: m['status']='failed';m['cleanup_error']=repr(error)
        m['cleanup']=cleanup;m['elapsed_seconds']=time.monotonic()-started;write(out/'manifest.json',m);checksums(out)
    require(m['status']=='passed','comparison and exact container cleanup required')
    print(json.dumps({'status':m['status'],'cases':len(results),'directory':str(out)}))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--run',type=pathlib.Path);parser.add_argument('--audit',type=pathlib.Path)
    for name in ('output','acquisition','load-root','stack-root','ordering-root'): parser.add_argument('--'+name,type=pathlib.Path)
    args=parser.parse_args()
    if args.run: execute(args.run)
    elif args.audit: print(json.dumps(audit(args.audit),indent=2))
    else:
        require(all(getattr(args,k) is not None for k in ('output','acquisition','load_root','stack_root','ordering_root')),'prepare arguments required')
        prepare(args)

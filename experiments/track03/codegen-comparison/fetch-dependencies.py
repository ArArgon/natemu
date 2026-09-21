#!/usr/bin/env python3
"""Retain only the eight FEX build gitlinks, with bounded primary-source fetches."""
import configparser
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile
import time
import urllib.request

sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent
REPO=HERE.parents[2]
helper=REPO/'experiments/track01/source/run.py'
spec=importlib.util.spec_from_file_location('retention',helper)
r=importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
run=r.allocate(os.environ.get('NATEMU_RAW_ROOT'),REPO,sys.argv[1])
source=run/'source'; source.mkdir()
shutil.copyfile(__file__,source/'fetch-dependencies.py')
shutil.copyfile(helper,source/'retention.py')
archive=Path(sys.argv[2]).resolve()
shutil.copyfile(archive,source/'upstream.tar.gz')
tcg = sys.argv[3:] == ['tcg']
pin = 'c1c18d1e640b64292859ce9f30f3c344edfb0294' if tcg else '48d71752e2a363d5535181f803b0b0e9666c1825'
record=dict(schema='natemu.track03.fex-dependencies.v1',purpose='exploratory',status='preparing',
            pin=pin,commands=[],downloads=[],dependencies=[],started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
deadline=time.monotonic()+300
budget=512*1024**2

def fetch(url,name):
    global budget
    target=source/name
    item=dict(url=url,file=name,bytes=0); record['downloads'].append(item)
    r.save(run,record)
    with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'natemu-research'}),timeout=30) as response, target.open('xb') as out:
        while True:
            chunk=response.read(1024*1024)
            if not chunk: break
            budget-=len(chunk); item['bytes']+=len(chunk)
            if budget<0 or item['bytes']>128*1024**2 or time.monotonic()>deadline:
                raise RuntimeError('dependency transfer/time limit')
            out.write(chunk)
    item['sha256']=hashlib.sha256(target.read_bytes()).hexdigest()
    r.save(run,record)
    return target

try:
    if tcg:
        with tarfile.open(source/'upstream.tar.gz') as tar:
            wrap=next(m for m in tar.getmembers() if m.name.endswith('/subprojects/keycodemapdb.wrap'))
            config=configparser.ConfigParser(); config.read_string(tar.extractfile(wrap).read().decode())
        revision=config['wrap-git']['revision']; url=config['wrap-git']['url']
        config=configparser.ConfigParser()
        config.read_dict({'keycodemapdb':{'path':'subprojects/keycodemapdb','url':url}})
        links={'subprojects/keycodemapdb':revision}; wanted=set(links)
    else:
        with tarfile.open(source/'upstream.tar.gz') as tar:
            module=next(m for m in tar.getmembers() if m.name.endswith('/.gitmodules') and m.name.count('/')==1)
            config=configparser.ConfigParser(); config.read_string(tar.extractfile(module).read().decode())
        metadata=source/'git-metadata'
        r.command(run,record,['git','init',str(metadata)])
        r.command(run,record,['git','-C',str(metadata),'fetch','--depth=1','--filter=blob:none',
                               'https://github.com/FEX-Emu/FEX.git',pin],timeout=90)
        listing=r.command(run,record,['git','-C',str(metadata),'ls-tree','-r','FETCH_HEAD'])
        links={line.split('\t',1)[1]:line.split()[2] for line in listing.splitlines()
               if line.startswith('160000 commit ')}
        record['git_metadata_bytes']=sum(p.stat().st_size for p in metadata.rglob('*') if p.is_file())
        budget-=record['git_metadata_bytes']
        if budget<0: raise RuntimeError('git metadata exceeds dependency budget')
        wanted={'External/vixl','Source/Common/cpp-optparse','External/fmt','External/xxhash',
                'External/jemalloc_glibc','External/range-v3','External/unordered_dense','External/rpmalloc'}
    with tarfile.open(source/'dependency-overlay.tar','x') as overlay:
        for section in config.sections():
            path=config[section]['path']
            if path not in wanted: continue
            repo=config[section]['url'].removeprefix('https://github.com/').removesuffix('.git')
            commit=links[path]
            url=('https://gitlab.com/qemu-project/keycodemapdb/-/archive/'+commit+'/keycodemapdb-'+commit+'.tar.gz'
                 if tcg else 'https://codeload.github.com/'+repo+'/tar.gz/'+commit)
            body=fetch(url,path.replace('/','_')+'.tar.gz')
            with tarfile.open(body) as tar:
                for member in tar:
                    parts=PurePosixPath(member.name).parts
                    if member.name.startswith('/') or '..' in parts: raise ValueError('unsafe archive member')
                    if len(parts)<2: continue
                    item=copy.copy(member); item.name=path+'/'+str(PurePosixPath(*parts[1:]))
                    overlay.addfile(item,tar.extractfile(member) if member.isfile() else None)
            record['dependencies'].append(dict(path=path,repo=repo,pin=commit,archive=body.name))
    if {d['path'] for d in record['dependencies']}!=wanted: raise ValueError('missing dependency')
    (source/'dependencies.json').write_text(json.dumps(record['dependencies'],indent=2)+'\n')
    record['status']='dependencies_retained_not_built'
except BaseException as error:
    record['status']='failed'; record['error']=type(error).__name__+': '+str(error)
    raise
finally:
    record['finished_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    r.save(run,record); r.checksums(run); print(run,flush=True)

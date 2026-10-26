#!/usr/bin/env python3
"""Retain discovery and checked single-instruction protected32 compilations."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys

OFFSETS=(0,5,10,12,17,22,24,29,35,37)

def probe(root,producer):
    root=Path(root).resolve();root.mkdir()
    source=Path(__file__).resolve().parent
    snapshot=root/'source';snapshot.mkdir()
    for name in ('compile.py','export.py','producer.json','probe_steps.py'):
        shutil.copyfile(source/name,snapshot/name)
    fixture=source.parents[1]/'fixtures/i4v/cases.json'
    shutil.copyfile(fixture,snapshot/'cases.json')
    shutil.copyfile(producer,root/'CodeSizeValidation')
    (root/'CodeSizeValidation').chmod(0o755)
    sys.path.insert(0,str(snapshot))
    from compile import compile_source
    spec=next(c for c in json.loads(fixture.read_text())['cases'] if c['id']=='fex_moffs_load32')
    full=bytes.fromhex(spec['x86_hex'])
    result=dict(schema='natemu-i4v-off32-steps-probe-v1',status='running',steps=[])
    try:
        for step,(start,end) in enumerate(zip(OFFSETS,OFFSETS[1:])):
            raw=full[start:end]
            discovery=root/f'step-{step}-discovery'
            compile_source(discovery,root/'CodeSizeValidation',raw,0x102000+start,32,1,0)
            log=(discovery/'producer.log').read_text()
            counts=re.findall(r"Testing instruction 'natemu-i4v-request': (\d+) host instructions",log)
            if not counts or len(set(counts))!=1: raise ValueError('ambiguous native instruction count')
            count=int(counts[0]);record=dict(step=step,offset=start,source_hex=raw.hex(),host_count=count,outputs=[])
            for label,base in [('low',0x102000),('shifted',0x102080)]:
                directory=root/f'step-{step}-{label}'
                compiled=compile_source(directory,root/'CodeSizeValidation',raw,base+start,32,1,count)
                native=(directory/'outputs/001.native').read_bytes()
                body=native[12:12+4*count]
                if native[12+4*count:16+4*count].hex()!='0f420000': raise ValueError('unexpected body boundary')
                record['outputs'].append(dict(directory=directory.name,guest_rip=base+start,
                    body_hex=body.hex(),object=compiled['object'],guest_pc=compiled['guest_pc']))
            result['steps'].append(record)
            (root/'probe.json').write_text(json.dumps(result,indent=2)+'\n')
        result['status']='passed_compile_only'
    except BaseException as e:
        result.update(status='failed',error=str(e));raise
    finally:
        (root/'probe.json').write_text(json.dumps(result,indent=2)+'\n')
        hashes={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(root.rglob('*')) if p.is_file() and p.name!='checksums.json'}
        (root/'checksums.json').write_text(json.dumps(hashes,indent=2)+'\n')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True,type=Path)
    parser.add_argument('--producer',required=True,type=Path)
    args=parser.parse_args();probe(args.directory,args.producer)

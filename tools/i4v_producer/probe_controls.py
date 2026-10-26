#!/usr/bin/env python3
"""Retain generated protected32 control-value moves and ordinary memory operations."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys

VALUES=(0x10011,0x80010011,0x1000,0x3000,0,0x80010010,0x20010011,0xffffffff,0x600)
SOURCES={f'imm-{value:08x}':b'\xb8'+value.to_bytes(4,'little') for value in VALUES}
SOURCES.update(store_eax_ebx=bytes.fromhex('8903'),load_eax_ebx=bytes.fromhex('8b03'))

def probe(root,producer,rips,bitness=32,extra_sources=()):
    root=Path(root).resolve();root.mkdir()
    source=Path(__file__).resolve().parent;snapshot=root/'source';snapshot.mkdir()
    for name in ('compile.py','ownership.py','export.py','producer.json','probe_controls.py',*extra_sources):
        shutil.copyfile(source/name,snapshot/name)
    shutil.copyfile(producer,root/'CodeSizeValidation');(root/'CodeSizeValidation').chmod(0o755)
    sys.path.insert(0,str(snapshot))
    from compile import compile_source
    result=dict(schema='natemu-i4v-controls-probe-v1',status='running',rips=rips,bitness=bitness,variants=[])
    try:
        for name,raw in SOURCES.items():
            discovery=root/f'{name}-discovery'
            compile_source(discovery,root/'CodeSizeValidation',raw,rips[0],bitness,1,0)
            counts=re.findall(r"Testing instruction 'natemu-i4v-request': (\d+) host instructions",(discovery/'producer.log').read_text())
            if not counts or len(set(counts))!=1:raise ValueError('ambiguous native instruction count')
            count=int(counts[0]);variant=dict(name=name,source_hex=raw.hex(),host_count=count,outputs=[])
            for index,rip in enumerate(rips):
                directory=root/f'{name}-{index}'
                compiled=compile_source(directory,root/'CodeSizeValidation',raw,rip,bitness,1,count)
                native=(directory/'outputs/001.native').read_bytes();body=native[12:12+4*count]
                if native[12+4*count:16+4*count].hex()!='0f420000':raise ValueError('unexpected body boundary')
                variant['outputs'].append(dict(directory=directory.name,guest_rip=rip,body_hex=body.hex(),object=compiled['object'],guest_pc=compiled['guest_pc']))
            result['variants'].append(variant)
            (root/'probe.json').write_text(json.dumps(result,indent=2)+'\n')
        result['status']='passed_compile_only'
    except BaseException as error:
        result.update(status='failed',error=str(error));raise
    finally:
        (root/'probe.json').write_text(json.dumps(result,indent=2)+'\n')
        hashes={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(root.rglob('*')) if p.is_file() and p.name!='checksums.json'}
        (root/'checksums.json').write_text(json.dumps(hashes,indent=2)+'\n')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True,type=Path);parser.add_argument('--producer',required=True,type=Path)
    parser.add_argument('--rip',action='append',type=lambda s:int(s,0))
    parser.add_argument('--bitness',type=int,choices=(32,64),default=32)
    parser.add_argument('--value',action='append',type=lambda s:int(s,0))
    args=parser.parse_args()
    if args.value is not None:
        SOURCES={f'imm-{value:08x}':b'\xb8'+value.to_bytes(4,'little') for value in args.value}
    probe(args.directory,args.producer,args.rip or [0x2000],args.bitness)

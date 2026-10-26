#!/usr/bin/env python3
"""Compile one owner-bound mixed paging-mode32 source snapshot."""
import argparse
import json
from pathlib import Path
import struct
from i4v_producer.compile import compile_source
from i4v_producer.export import CASES, bounded, export_program, require, verify
MISS=0x49345641
DELIVER=0x49345642
RIP=0x102000
VARIANTS={1:'fex_mixed32_old',2:'fex_mixed32_changed'}

def request(vm,owner,incarnation,generation,context,mode,serial):
    require(vm>0 and 0<=owner<4 and incarnation>0 and generation in VARIANTS and context>0 and mode in (0,1) and serial>0,'unsupported mixed compile identity')
    source=bytes.fromhex(CASES[VARIANTS[generation]]['source'])
    return (MISS,1,vm,owner,incarnation,generation,context,RIP,mode,int.from_bytes(source.ljust(8,b'\0'),'little'),RIP,serial,0,0,0,0)

def parse_request(raw):
    require(len(raw)==128,'mixed request must contain sixteen words')
    p=struct.unpack('<16Q',raw)
    require(p==request(p[2],p[3],p[4],p[5],p[6],p[8],p[11]),'mixed source/mode/address/identity mismatch')
    return p

def make_reply(packet,body):
    parse_request(struct.pack('<16Q',*packet))
    require(body==bytes.fromhex(CASES[VARIANTS[packet[5]]]['body']),'mixed emitted body mismatch')
    out=list(packet);out[0]=DELIVER;out[13:16]=struct.unpack('<3Q',body.ljust(24,b'\0'))
    return struct.pack('<16Q',*out)

def compile_request(directory,producer):
    directory=Path(directory).resolve();packet=parse_request(bounded(directory/'request.bin',128))
    case=VARIANTS[packet[5]];spec=CASES[case]
    compile_source(directory/'compile',Path(producer),bytes.fromhex(spec['source']),packet[7],32,3,5)
    export_program(directory/'compile',case)
    program=verify(directory/'compile',dict(case=case,guest_rip=packet[7],bitness=32,source_hex=spec['source']))
    raw=make_reply(packet,bytes.fromhex(program['body_hex']))
    with (directory/'reply.bin').open('xb') as f:f.write(raw)
    record=dict(schema='natemu-i4v-mixed32-worker-v1',request=packet,reply=struct.unpack('<16Q',raw),producer_sha256=program['producer_sha256'],body_sha256=program['body_sha256'])
    with (directory/'worker.json').open('x') as f:json.dump(record,f,indent=2);f.write('\n')
    verify_request(directory,packet)

def verify_request(directory,expected):
    directory=Path(directory);packet=parse_request(bounded(directory/'request.bin',128))
    require(list(packet)==list(expected),'mixed compilation differs from resident request')
    case=VARIANTS[packet[5]];spec=CASES[case]
    program=verify(directory/'compile',dict(case=case,guest_rip=packet[7],bitness=32,source_hex=spec['source']))
    raw=make_reply(packet,bytes.fromhex(program['body_hex']))
    require(bounded(directory/'reply.bin',128)==raw,'mixed reply differs from actual compilation')
    record=dict(schema='natemu-i4v-mixed32-worker-v1',request=list(packet),reply=list(struct.unpack('<16Q',raw)),producer_sha256=program['producer_sha256'],body_sha256=program['body_sha256'])
    require(json.loads(bounded(directory/'worker.json',8192))==record,'mixed worker provenance mismatch')
    return program

def verify_compilations(root,requests,replies=None):
    dirs=sorted(Path(root).glob('request-*'));require(len(dirs)==len(requests),'mixed compilation count differs')
    if replies is not None:require(len(replies)==len(requests),'mixed delivery count differs')
    for index,(directory,packet) in enumerate(zip(dirs,requests)):
        require(directory.name==f'request-{index:03}','mixed compilation order differs')
        verify_request(directory,packet)
        if replies is not None:require(bounded(directory/'reply-delivered.bin',128)==struct.pack('<16Q',*replies[index]),'mixed delivered reply mismatch')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--request-dir',required=True,type=Path);p.add_argument('--producer',required=True,type=Path)
    args=p.parse_args();compile_request(args.request_dir,args.producer)

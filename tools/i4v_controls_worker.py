#!/usr/bin/env python3
"""Compile one owner-bound control paging-mode32 source snapshot."""
import argparse
import json
from pathlib import Path
import struct
from i4v_producer.compile import compile_source
from i4v_producer.export import CASES, bounded, export_program, require, verify
MISS=0x49345651
DELIVER=0x49345652
SOURCE_CASES={bytes.fromhex(spec['source']):name for name,spec in CASES.items()
              if name.startswith('fex_mov_eax_') or name in ('fex_store_eax_ebx32','fex_load_eax_ebx32')}

def request(vm,generation,mode,rip,source,serial):
    require(vm>0 and generation>0 and mode in (0,1) and serial>0,'unsupported control compile identity')
    require(source in SOURCE_CASES and 0x102000<=rip and rip+len(source)<=0x103000,'unsupported ordinary source/address')
    body=bytes.fromhex(CASES[SOURCE_CASES[source]]['body'])
    return (MISS,1,vm,0,1,1,generation,rip,mode,int.from_bytes(source.ljust(8,b'\0'),'little'),len(source),rip,serial,len(body),0,0)

def source_of(packet):
    require(packet[10]<=8,'oversized control source')
    return packet[9].to_bytes(8,'little')[:packet[10]]

def parse_request(raw):
    require(len(raw)==128,'control request must contain sixteen words')
    p=struct.unpack('<16Q',raw)
    require(p==request(p[2],p[6],p[8],p[7],source_of(p),p[12]),'control source/mode/address/identity mismatch')
    return p

def make_reply(packet,body):
    parse_request(struct.pack('<16Q',*packet))
    require(body==bytes.fromhex(CASES[SOURCE_CASES[source_of(packet)]]['body']),'control emitted body mismatch')
    out=list(packet);out[0]=DELIVER;out[14:16]=struct.unpack('<2Q',body.ljust(16,b'\0'))
    return struct.pack('<16Q',*out)

def compile_request(directory,producer):
    directory=Path(directory).resolve();packet=parse_request(bounded(directory/'request.bin',128))
    case=SOURCE_CASES[source_of(packet)];spec=CASES[case]
    compile_source(directory/'compile',Path(producer),bytes.fromhex(spec['source']),packet[7],32,1,spec['host_count'])
    export_program(directory/'compile',case)
    program=verify(directory/'compile',dict(case=case,guest_rip=packet[7],bitness=32,source_hex=spec['source']))
    raw=make_reply(packet,bytes.fromhex(program['body_hex']))
    with (directory/'reply.bin').open('xb') as f:f.write(raw)
    record=dict(schema='natemu-i4v-controls-worker-v1',request=packet,reply=struct.unpack('<16Q',raw),producer_sha256=program['producer_sha256'],body_sha256=program['body_sha256'])
    with (directory/'worker.json').open('x') as f:json.dump(record,f,indent=2);f.write('\n')
    verify_request(directory,packet)

def verify_request(directory,expected):
    directory=Path(directory);packet=parse_request(bounded(directory/'request.bin',128))
    require(list(packet)==list(expected),'control compilation differs from resident request')
    case=SOURCE_CASES[source_of(packet)];spec=CASES[case]
    program=verify(directory/'compile',dict(case=case,guest_rip=packet[7],bitness=32,source_hex=spec['source']))
    raw=make_reply(packet,bytes.fromhex(program['body_hex']))
    require(bounded(directory/'reply.bin',128)==raw,'control reply differs from actual compilation')
    record=dict(schema='natemu-i4v-controls-worker-v1',request=list(packet),reply=list(struct.unpack('<16Q',raw)),producer_sha256=program['producer_sha256'],body_sha256=program['body_sha256'])
    require(json.loads(bounded(directory/'worker.json',8192))==record,'control worker provenance mismatch')
    return program

def verify_compilations(root,requests,replies=None):
    dirs=sorted(Path(root).glob('request-*'));require(len(dirs)==len(requests),'control compilation count differs')
    if replies is not None:require(len(replies)==len(requests),'control delivery count differs')
    for index,(directory,packet) in enumerate(zip(dirs,requests)):
        require(directory.name==f'request-{index:03}','control compilation order differs')
        verify_request(directory,packet)
        if replies is not None:require(bounded(directory/'reply-delivered.bin',128)==struct.pack('<16Q',*replies[index]),'control delivered reply mismatch')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--request-dir',required=True,type=Path);p.add_argument('--producer',required=True,type=Path)
    args=p.parse_args();compile_request(args.request_dir,args.producer)

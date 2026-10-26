#!/usr/bin/env python3
"""Compile the I4 packet's actual low/high RIP with the pinned I4-V producer."""
import argparse
import json
from pathlib import Path
import struct

from i4v_producer.compile import compile_source
from i4v_producer.export import CASES, bounded, export_program, require, verify

SOURCES={bytes.fromhex(CASES[name]['source']):name for name in ('fex_alias_xor64','fex_alias_or64')}

def parse_request(raw):
    require(len(raw)==128,'alias request must be sixteen words')
    p=struct.unpack('<16Q',raw)
    require(p[:2]==(11,3) and all(p[n]>0 for n in (2,3,4,5)),'invalid alias request identity')
    require(p[6]==17 and p[10:12]==(3,0),'unsupported alias source/profile/owner')
    require(p[12] in (0x102000,0xffff800000102000),'unsupported alias guest RIP')
    padded=struct.pack('<3Q',*p[7:10]);source=padded[:17]
    require(not any(padded[17:]) and source in SOURCES,'unsupported alias source/padding')
    require(p[13]>0 and p[15]>0,'missing alias request count/serial')
    return p,source

def make_reply(request,body):
    _,source=parse_request(struct.pack('<16Q',*request))
    require(body==bytes.fromhex(CASES[SOURCES[source]]['body']),'alias body differs from emitted profile')
    p=[2,3,*request[2:6],44,*struct.unpack('<6Q',body+bytes(4)),request[12],64,3]
    return struct.pack('<16Q',*p)

def compile_request(directory,producer):
    directory=Path(directory).resolve();request,source=parse_request(bounded(directory/'request.bin',128))
    compile_source(directory/'compile',Path(producer),source,request[12],64,5,11)
    export_program(directory/'compile',SOURCES[source])
    program=verify(directory/'compile',dict(case=SOURCES[source],guest_rip=request[12],source_hex=source.hex()))
    reply=make_reply(request,bytes.fromhex(program['body_hex']))
    with (directory/'reply.bin').open('xb') as stream:stream.write(reply)
    with (directory/'worker.json').open('x') as stream:
        json.dump(dict(schema='natemu-i4v-alias-worker-v1',request=list(request),reply=list(struct.unpack('<16Q',reply)),
                       producer_sha256=program['producer_sha256'],body_sha256=program['body_sha256']),stream,indent=2);stream.write('\n')
    verify_request(directory,request)

def verify_request(directory,expected):
    directory=Path(directory);request,source=parse_request(bounded(directory/'request.bin',128))
    require(list(request)==list(expected),'alias compilation differs from resident request')
    program=verify(directory/'compile',dict(case=SOURCES[source],guest_rip=request[12],source_hex=source.hex()))
    reply=make_reply(request,bytes.fromhex(program['body_hex']))
    require(bounded(directory/'reply.bin',128)==reply,'alias reply differs from compilation/request')
    record=dict(schema='natemu-i4v-alias-worker-v1',request=list(request),reply=list(struct.unpack('<16Q',reply)),
                producer_sha256=program['producer_sha256'],body_sha256=program['body_sha256'])
    require(json.loads(bounded(directory/'worker.json',8192))==record,'alias worker record differs')
    return program

def verify_compilations(root,requests):
    directories=sorted(Path(root).glob('request-*'))
    require(len(directories)==len(requests),'alias compile count differs')
    for index,(directory,request) in enumerate(zip(directories,requests)):
        require(directory.name==f'request-{index:03}','alias compile directory order differs')
        verify_request(directory,request)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--request-dir',required=True,type=Path);p.add_argument('--producer',required=True,type=Path)
    a=p.parse_args();compile_request(a.request_dir,a.producer)

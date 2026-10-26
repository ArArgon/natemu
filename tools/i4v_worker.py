#!/usr/bin/env python3
"""Compile one actual resident I4-V request; bind the reply to its identity."""
import argparse
import json
from pathlib import Path
import struct

from i4v_producer.compile import compile_source
from i4v_producer.export import bounded, export_program, require, verify

MISS = 0x49345601
COMPILED = 0x49345602
SOURCE = bytes.fromhex('488b03')
BODY = bytes.fromhex('c4fcdfc81f2003d5')

def parse_request(raw):
    require(len(raw)==128, 'request must contain exactly sixteen words')
    p=struct.unpack('<16Q',raw)
    require(p[0]==MISS and p[1]==1 and p[2]>0, 'invalid request version/VM identity')
    require(p[3:7]==(0,1,1,1), 'unsupported vCPU/incarnation/source/context generation')
    require(p[7] in (0x102000,0xffff800000102000), 'unsupported guest RIP')
    require(p[8:]==(64,3,0x038b48,0,0,1,0,0), 'invalid mode/source/padding/serial')
    return p

def make_reply(request, body):
    parse_request(struct.pack('<16Q',*request))
    require(body==BODY,'unsupported generated body')
    reply=list(request);reply[0]=COMPILED;reply[11]=len(body)
    reply[12]=int.from_bytes(body,'little')
    return struct.pack('<16Q',*reply)

def compile_request(directory, producer):
    directory=Path(directory).resolve()
    raw=bounded(directory/'request.bin',128);request=parse_request(raw)
    compile_source(directory/'compile',Path(producer),SOURCE,request[7],64,1,2)
    export_program(directory/'compile','fex_memory_data64')
    program=verify(directory/'compile',dict(case='fex_memory_data64',guest_rip=request[7],bitness=64,source_hex=SOURCE.hex()))
    reply=make_reply(request,bytes.fromhex(program['body_hex']))
    with (directory/'reply.bin').open('xb') as stream:stream.write(reply)
    with (directory/'worker.json').open('x') as stream:
        json.dump(dict(schema='natemu-i4v-worker-v1',request=list(request),reply=list(struct.unpack('<16Q',reply)),
                       producer_sha256=program['producer_sha256'],body_sha256=program['body_sha256']),stream,indent=2)
        stream.write('\n')
    verify_request(directory,request)

def verify_request(directory, expected_request):
    directory=Path(directory)
    request=parse_request(bounded(directory/'request.bin',128))
    require(list(request)==list(expected_request),'worker input differs from resident request')
    program=verify(directory/'compile',dict(case='fex_memory_data64',guest_rip=request[7],bitness=64,source_hex=SOURCE.hex()))
    reply=make_reply(request,bytes.fromhex(program['body_hex']))
    require(bounded(directory/'reply.bin',128)==reply,'reply differs from actual compiled body/request')
    record=json.loads(bounded(directory/'worker.json',8192))
    require(record==dict(schema='natemu-i4v-worker-v1',request=list(request),reply=list(struct.unpack('<16Q',reply)),
                        producer_sha256=program['producer_sha256'],body_sha256=program['body_sha256']), 'worker record differs from producing artifacts')
    return program

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--request-dir',required=True,type=Path);p.add_argument('--producer',required=True,type=Path)
    a=p.parse_args();compile_request(a.request_dir,a.producer)

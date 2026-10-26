#!/usr/bin/env python3
"""Compile one resident protected32 paging-off request and retain three request-bound chunks."""
import argparse
import json
from pathlib import Path
import struct

from i4v_producer.compile import compile_source
from i4v_producer.export import CASES, bounded, export_program, require, verify

MISS = 0x49345621
COMPILED = 0x49345622
CASE = 'fex_moffs_load32'
SOURCE = bytes.fromhex(CASES[CASE]['source'])
BODY = bytes.fromhex(CASES[CASE]['body'])
SOURCE_WORDS = struct.unpack('<5Q',SOURCE.ljust(40,b'\0'))

def request_source(request):
    return struct.pack('<5Q',*request[9:14])[:37]

def parse_request(raw):
    require(len(raw)==128,'paging-off request must contain exactly sixteen words')
    p=struct.unpack('<16Q',raw)
    require(p[0]==MISS and p[1]==1 and p[2]>0,'invalid paging-off request version/VM')
    require(p[3:7]==(0,1,1,1) and p[14]==1,'unsupported paging-off participant/generations/serial')
    require(p[7] in (0x102000,0x102080) and p[8]==32,'unsupported paging-off guest RIP/mode')
    require(p[9:14]==SOURCE_WORDS and p[15]<3,'invalid paging-off source/padding/chunk')
    return p

def make_reply(request,body):
    parse_request(struct.pack('<16Q',*request))
    require(body==BODY,'unsupported paging-off body')
    chunk=request[15];part=body[24*chunk:24*(chunk+1)]
    reply=list(request);reply[0]=COMPILED
    reply[9:12]=struct.unpack('<3Q',part.ljust(24,b'\0'))
    reply[12]=chunk;reply[13]=len(part);reply[15]=len(body)
    return struct.pack('<16Q',*reply)

def requests_for(first):
    return [tuple(list(first[:15])+[chunk]) for chunk in range(3)]

def compile_request(directory,producer):
    directory=Path(directory).resolve();first=parse_request(bounded(directory/'request.bin',128))
    require(first[15]==0,'compilation must begin with chunk zero')
    source=request_source(first);case=CASE
    compile_source(directory/'compile',Path(producer),source,first[7],32,9,16)
    export_program(directory/'compile',case)
    program=verify(directory/'compile',dict(case=case,guest_rip=first[7],bitness=32,source_hex=source.hex()))
    requests=requests_for(first);replies=[]
    for chunk,request in enumerate(requests):
        reply=make_reply(request,bytes.fromhex(program['body_hex']));replies.append(list(struct.unpack('<16Q',reply)))
        with (directory/f'reply-{chunk}.bin').open('xb') as stream:stream.write(reply)
    record=dict(schema='natemu-i4v-paging-off-worker-v1',requests=requests,replies=replies,
                producer_sha256=program['producer_sha256'],body_sha256=program['body_sha256'])
    with (directory/'worker.json').open('x') as stream:json.dump(record,stream,indent=2);stream.write('\n')
    verify_request(directory,first)

def verify_request(directory,expected_first):
    directory=Path(directory);first=parse_request(bounded(directory/'request.bin',128))
    require(first[15]==0 and list(first)==list(expected_first),'paging-off compilation differs from first resident request')
    source=request_source(first);case=CASE
    program=verify(directory/'compile',dict(case=case,guest_rip=first[7],bitness=32,source_hex=source.hex()))
    requests=requests_for(first);replies=[]
    for chunk,request in enumerate(requests):
        reply=make_reply(request,bytes.fromhex(program['body_hex']))
        require(bounded(directory/f'reply-{chunk}.bin',128)==reply,'paging-off reply chunk differs from compilation/request')
        replies.append(list(struct.unpack('<16Q',reply)))
    expected=dict(schema='natemu-i4v-paging-off-worker-v1',requests=[list(p) for p in requests],replies=replies,
                  producer_sha256=program['producer_sha256'],body_sha256=program['body_sha256'])
    require(json.loads(bounded(directory/'worker.json',8192))==expected,'paging-off worker record differs from producing artifacts')
    return program

def verify_compilations(root,requests):
    directories=sorted(Path(root).glob('request-*'))
    require(len(directories)==len(requests),'paging-off compilation count differs')
    for index,(directory,request) in enumerate(zip(directories,requests)):
        require(directory.name==f'request-{index:03}','paging-off compilation directory order differs')
        verify_request(directory,request)

def verify_transfers(directory,expected_requests,expected_replies=None):
    require(1<=len(expected_requests)<=3,'paging-off transfer requires one to three observed requests')
    if expected_replies is not None:
        require(len(expected_replies)==len(expected_requests),'delivered paging-off reply count differs')
    program=verify_request(directory,expected_requests[0])
    for chunk,expected in enumerate(expected_requests):
        actual=parse_request(bounded(Path(directory)/f'request-{chunk}.bin',128))
        require(list(actual)==list(expected) and actual[15]==chunk,'paging-off transfer differs from resident request')
        require(actual==requests_for(tuple(expected_requests[0]))[chunk],'paging-off transfer changes compile identity')
        if expected_replies is not None:
            delivered=bounded(Path(directory)/f'reply-delivered-{chunk}.bin',128)
            require(delivered==struct.pack('<16Q',*expected_replies[chunk]),'delivered reply differs from resident observation')
    return program

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--request-dir',required=True,type=Path);p.add_argument('--producer',required=True,type=Path)
    a=p.parse_args();compile_request(a.request_dir,a.producer)

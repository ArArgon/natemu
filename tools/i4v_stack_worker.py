#!/usr/bin/env python3
"""Compile one resident stack request and retain three request-bound chunks."""
import argparse
import json
from pathlib import Path
import struct

from i4v_producer.compile import compile_source
from i4v_producer.export import bounded, export_program, require, verify

MISS = 0x49345611
COMPILED = 0x49345612
SOURCE = bytes.fromhex('68bbbcbdbe6668525166686261488b42f88b5af4')
BODY = bytes.fromhex('94688892b4d7b7f2148d1ff8542a8a5214ed1f78542c8c5214ed1f78b42000d184fedfc81f2003d5b43000d186fedf881f2003d5')
SOURCE_WORDS = struct.unpack('<3Q',SOURCE.ljust(24,b'\0'))

def parse_request(raw):
    require(len(raw)==128,'stack request must contain exactly sixteen words')
    p=struct.unpack('<16Q',raw)
    require(p[0]==MISS and p[1]==1 and p[2]>0,'invalid stack request version/VM')
    require(p[3:7]==(0,1,1,1),'unsupported stack participant/generations')
    require(p[7] in (0x102000,0xffff800000102000),'unsupported stack guest RIP')
    require(p[8:15]==(64,20,*SOURCE_WORDS,1,52) and 0<=p[15]<3,'invalid stack mode/source/serial/chunk')
    return p

def make_reply(request,body):
    parse_request(struct.pack('<16Q',*request));require(body==BODY,'unsupported stack body')
    chunk=request[15];part=body[24*chunk:24*(chunk+1)]
    reply=list(request);reply[0]=COMPILED;reply[9]=len(body)
    reply[10:13]=struct.unpack('<3Q',part.ljust(24,b'\0'))
    reply[14]=chunk;reply[15]=len(part)
    return struct.pack('<16Q',*reply)

def requests_for(first):
    return [tuple(list(first[:15])+[chunk]) for chunk in range(3)]

def compile_request(directory,producer):
    directory=Path(directory).resolve();first=parse_request(bounded(directory/'request.bin',128))
    require(first[15]==0,'compilation must begin with chunk zero')
    compile_source(directory/'compile',Path(producer),SOURCE,first[7],64,5,13)
    export_program(directory/'compile','fex_push_immediates64')
    program=verify(directory/'compile',dict(case='fex_push_immediates64',guest_rip=first[7],source_hex=SOURCE.hex()))
    requests=requests_for(first);replies=[]
    for chunk,request in enumerate(requests):
        reply=make_reply(request,bytes.fromhex(program['body_hex']));replies.append(list(struct.unpack('<16Q',reply)))
        with (directory/f'reply-{chunk}.bin').open('xb') as stream:stream.write(reply)
    record=dict(schema='natemu-i4v-stack-worker-v1',requests=requests,replies=replies,
                producer_sha256=program['producer_sha256'],body_sha256=program['body_sha256'])
    with (directory/'worker.json').open('x') as stream:json.dump(record,stream,indent=2);stream.write('\n')
    verify_request(directory,first)

def verify_request(directory,expected_first):
    directory=Path(directory);first=parse_request(bounded(directory/'request.bin',128))
    require(first[15]==0 and list(first)==list(expected_first),'stack compilation differs from first resident request')
    program=verify(directory/'compile',dict(case='fex_push_immediates64',guest_rip=first[7],source_hex=SOURCE.hex()))
    requests=requests_for(first);replies=[]
    for chunk,request in enumerate(requests):
        reply=make_reply(request,bytes.fromhex(program['body_hex']))
        require(bounded(directory/f'reply-{chunk}.bin',128)==reply,'stack reply chunk differs from compilation/request')
        replies.append(list(struct.unpack('<16Q',reply)))
    expected=dict(schema='natemu-i4v-stack-worker-v1',requests=[list(p) for p in requests],replies=replies,
                  producer_sha256=program['producer_sha256'],body_sha256=program['body_sha256'])
    require(json.loads(bounded(directory/'worker.json',8192))==expected,'stack worker record differs from producing artifacts')
    return program

def verify_transfers(directory,expected_requests,expected_replies=None):
    require(1<=len(expected_requests)<=3,'stack transfer requires one to three observed requests')
    if expected_replies is not None:
        require(len(expected_replies)==len(expected_requests),'delivered stack reply count differs')
    program=verify_request(directory,expected_requests[0])
    for chunk,expected in enumerate(expected_requests):
        actual=parse_request(bounded(Path(directory)/f'request-{chunk}.bin',128))
        require(list(actual)==list(expected) and actual[15]==chunk,'stack transfer differs from resident request')
        require(actual==requests_for(tuple(expected_requests[0]))[chunk],'stack transfer changes compile identity')
        if expected_replies is not None:
            delivered=bounded(Path(directory)/f'reply-delivered-{chunk}.bin',128)
            require(delivered==struct.pack('<16Q',*expected_replies[chunk]),'delivered reply differs from resident observation')
    return program

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--request-dir',required=True,type=Path);p.add_argument('--producer',required=True,type=Path)
    a=p.parse_args();compile_request(a.request_dir,a.producer)

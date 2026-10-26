#!/usr/bin/env python3
"""One whole FEX dependency compilation with four bound body deliveries."""
import argparse
import json
from pathlib import Path
import struct
from i4v_address_worker import compiler_slot
from i4v_producer.compile import compile_source
from i4v_producer.export import CASES, bounded, export_program, require, verify

CASE='fex_lfence_dependencies'
MISS,DELIVER=0x49345691,0x49345692

def request(vm,n,case,owner,chunk):
    require(all(type(x) is int for x in (vm,n,case,owner,chunk)), 'integer fence identities required')
    require(0<vm<1<<64 and 1<=n<=4 and 0<=case<5 and 0<=owner<n and 0<=chunk<4,'invalid fence owner/profile/chunk')
    spec=CASES[CASE];source=bytes.fromhex(spec['source'])
    return (MISS,1,vm,owner,1,1,(n<<8)|(case+1),0x102000+owner*256,64,*struct.unpack('<4Q',source.ljust(32,b'\0')),1,120,chunk)

def parse_request(raw):
    require(len(raw)==128,'fence request must contain16words')
    p=struct.unpack('<16Q',raw);context=p[6]
    require(p==request(p[2],context>>8,(context&255)-1,p[3],p[15]),'fence source or owner identity mismatch')
    return p

def make_replies(p,body):
    parse_request(struct.pack('<16Q',*p))
    require(p[15]==0 and body==bytes.fromhex(CASES[CASE]['body']) and len(body)==120,'whole fence compilation differs')
    replies=[]
    for chunk in range(4):
        out=list(request(p[2],p[6]>>8,(p[6]&255)-1,p[3],chunk));out[0]=DELIVER
        out[9:13]=struct.unpack('<4Q',body[chunk*32:(chunk+1)*32].ljust(32,b'\0'))
        replies.append(struct.pack('<16Q',*out))
    return replies

def compile_request(directory,producer):
    directory=Path(directory).resolve();p=parse_request(bounded(directory/'request.bin',128))
    require(directory.parent.name==f'cpu-{p[3]}' and directory.name=='request-000' and p[15]==0,'fence compilation directory/chunk differs')
    spec=CASES[CASE]
    with compiler_slot(directory.parent.parent):
        compile_source(directory/'compile',Path(producer),bytes.fromhex(spec['source']),p[7],64,spec['instructions'],spec['host_count'])
    export_program(directory/'compile',CASE)
    program=verify(directory/'compile',dict(case=CASE,guest_rip=p[7],bitness=64,source_hex=spec['source']))
    replies=make_replies(p,bytes.fromhex(program['body_hex']))
    for i,raw in enumerate(replies):
        with (directory/f'reply-{i}.bin').open('xb') as stream:stream.write(raw)
    record=dict(schema='natemu-i4v-fence-worker-v1',request=p,replies=[struct.unpack('<16Q',raw) for raw in replies],producer_sha256=program['producer_sha256'],body_sha256=program['body_sha256'])
    with (directory/'worker.json').open('x') as stream:json.dump(record,stream,indent=2);stream.write('\n')
    verify_request(directory,p)

def verify_request(directory,expected):
    directory=Path(directory);p=parse_request(bounded(directory/'request.bin',128))
    require(list(p)==list(expected),'actual fence compiler request differs')
    spec=CASES[CASE]
    program=verify(directory/'compile',dict(case=CASE,guest_rip=p[7],bitness=64,source_hex=spec['source']))
    replies=make_replies(p,bytes.fromhex(program['body_hex']))
    for i,raw in enumerate(replies):require(bounded(directory/f'reply-{i}.bin',128)==raw,'fence body chunk differs')
    record=dict(schema='natemu-i4v-fence-worker-v1',request=list(p),replies=[list(struct.unpack('<16Q',raw)) for raw in replies],producer_sha256=program['producer_sha256'],body_sha256=program['body_sha256'])
    require(json.loads(bounded(directory/'worker.json',8192))==record,'fence worker record differs')
    return program

def verify_compilations(root,requests,replies):
    directories=sorted(Path(root).glob('request-*'))
    require(len(directories)==1 and len(requests)==len(replies)==4,'fence requires one compile and four deliveries')
    directory=directories[0];program=verify_request(directory,requests[0]);p=tuple(requests[0])
    expected=make_replies(p,bytes.fromhex(program['body_hex']))
    for i,raw in enumerate(expected):
        require(list(request(p[2],p[6]>>8,(p[6]&255)-1,p[3],i))==requests[i],'resident fence chunk request differs')
        require(struct.pack('<16Q',*replies[i])==raw,'resident fence body differs')
        if i:require(bounded(directory/f'request-chunk-{i}.bin',128)==struct.pack('<16Q',*requests[i]),'delivered chunk request differs')
        require(bounded(directory/f'reply-delivered-{i}.bin',128)==raw,'actual fence delivery differs')
    return program

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--request-dir',required=True,type=Path);parser.add_argument('--producer',required=True,type=Path)
    args=parser.parse_args();compile_request(args.request_dir,args.producer)

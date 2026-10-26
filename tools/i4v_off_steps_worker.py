#!/usr/bin/env python3
"""Compile one exact protected32 instruction at the resident's logical EIP."""
import argparse
import json
from pathlib import Path
import struct
from i4v_producer.compile import compile_source
from i4v_producer.export import CASES, bounded, export_program, require, verify

MISS=0x49345631
DELIVER=0x49345632
OFFSETS=(0,5,10,12,17,22,24,29,35)
STEP_CASES=tuple(f'fex_moffs_load32_step{3 if i==6 else i}' for i in range(9))

def request(vm,profile,step):
    require(vm>0 and 0<=profile<10 and 0<=step<9,'unsupported request identity')
    spec=CASES[STEP_CASES[step]];source=bytes.fromhex(spec['source']);eip=0x2000+OFFSETS[step]
    limit={3:0x2010,4:0x2013,5:0x1fff}.get(profile,0xffffffff)
    require(eip+len(source)-1<=limit,'instruction fetch exceeds admitted CS limit')
    linear=0x300000+eip;gpa=linear & ~(1<<20) if profile==1 else linear
    return (MISS,1,vm,0,1,1,profile+1,eip,32,int.from_bytes(source.ljust(8,b'\0'),'little'),len(source),linear,gpa,step+1,len(bytes.fromhex(spec['body'])),step)

def parse_request(raw):
    require(len(raw)==128,'instruction request must contain sixteen words')
    packet=struct.unpack('<16Q',raw)
    require(packet==request(packet[2],packet[6]-1,packet[15]),'instruction request/source/mode/address mismatch')
    return packet

def make_reply(packet,body):
    parse_request(struct.pack('<16Q',*packet))
    require(body==bytes.fromhex(CASES[STEP_CASES[packet[15]]]['body']),'instruction body mismatch')
    reply=list(packet);reply[0]=DELIVER;reply[9:11]=struct.unpack('<2Q',body.ljust(16,b'\0'))
    return struct.pack('<16Q',*reply)

def compile_request(directory,producer):
    directory=Path(directory).resolve();packet=parse_request(bounded(directory/'request.bin',128))
    case=STEP_CASES[packet[15]];spec=CASES[case]
    compile_source(directory/'compile',Path(producer),bytes.fromhex(spec['source']),packet[7],32,1,spec['host_count'])
    export_program(directory/'compile',case)
    program=verify(directory/'compile',dict(case=case,guest_rip=packet[7],bitness=32,source_hex=spec['source']))
    raw=make_reply(packet,bytes.fromhex(program['body_hex']))
    with (directory/'reply.bin').open('xb') as stream:stream.write(raw)
    record=dict(schema='natemu-i4v-paging-off-steps-worker-v1',request=packet,reply=struct.unpack('<16Q',raw),
                producer_sha256=program['producer_sha256'],body_sha256=program['body_sha256'])
    with (directory/'worker.json').open('x') as stream:json.dump(record,stream,indent=2);stream.write('\n')
    verify_request(directory,packet)

def verify_request(directory,expected):
    directory=Path(directory);packet=parse_request(bounded(directory/'request.bin',128))
    require(list(packet)==list(expected),'compiled instruction differs from resident request')
    case=STEP_CASES[packet[15]];spec=CASES[case]
    program=verify(directory/'compile',dict(case=case,guest_rip=packet[7],bitness=32,source_hex=spec['source']))
    raw=make_reply(packet,bytes.fromhex(program['body_hex']))
    require(bounded(directory/'reply.bin',128)==raw,'instruction reply differs from compilation')
    record=dict(schema='natemu-i4v-paging-off-steps-worker-v1',request=list(packet),reply=list(struct.unpack('<16Q',raw)),
                producer_sha256=program['producer_sha256'],body_sha256=program['body_sha256'])
    require(json.loads(bounded(directory/'worker.json',8192))==record,'instruction worker record differs')
    return program

def verify_compilations(root,requests,replies=None):
    directories=sorted(Path(root).glob('request-*'))
    require(len(directories)==len(requests),'instruction compilation count differs')
    if replies is not None:require(len(replies)==len(requests),'instruction delivery count differs')
    for index,(directory,packet) in enumerate(zip(directories,requests)):
        require(directory.name==f'request-{index:03}' and packet[15]==index,'instruction compilation order differs')
        verify_request(directory,packet)
        if replies is not None:
            require(bounded(directory/'reply-delivered.bin',128)==struct.pack('<16Q',*replies[index]),'delivered instruction reply differs')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request-dir',required=True,type=Path);parser.add_argument('--producer',required=True,type=Path)
    args=parser.parse_args();compile_request(args.request_dir,args.producer)

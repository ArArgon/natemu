#!/usr/bin/env python3
"""Compile one whole ordering participant with exact owner/source binding."""
import argparse
import json
from pathlib import Path
import struct
from i4v_address_worker import compiler_slot
from i4v_producer.compile import compile_source
from i4v_producer.export import CASES, bounded, export_program, require, verify

MISS = 0x49345681
DELIVER = 0x49345682
NAMES = ('sb', 'mp_writer', 'reader', 'wrc_middle', 'writer', 'sb_mfence',
         'sb_xchg', 'xchg_result', 'lfence', 'sfence', 'mfence', 'xchg')

def variant(profile, role):
    if profile == 0: return 0
    if profile == 1: return 1 if role == 0 else 2
    if profile == 2: return 3
    if profile == 3: return (4, 3, 2)[role]
    if profile == 4: return 4 if role < 2 else 2
    if profile in (5, 6): return profile
    if profile in (7, 8, 9): return 7
    return profile - 2

def request(vm, n, profile, rotation, seed, owner):
    require(all(type(x) is int for x in (vm,n,profile,rotation,seed,owner)), 'integer identities required')
    require(0 < vm < 1 << 64 and 1 <= n <= 4 and 0 <= profile <= 12 and 0 <= rotation < n and 0 <= owner < n and 0 < seed <= 65535, 'invalid ordering identity')
    require(profile >= 7 or n == (2,2,2,3,4,2,2)[profile], 'wrong participant count')
    v = variant(profile, (owner+n-rotation)%n)
    spec = CASES['fex_order_'+NAMES[v]]
    source = bytes.fromhex(spec['source'])
    source_words = struct.unpack('<2Q', source.ljust(16,b'\0'))
    context = (profile+1)|(n<<8)|(rotation<<16)|(seed<<32)
    return (MISS,1,vm,owner,1,1,context,0x102000+owner*256,64,*source_words,len(source),len(bytes.fromhex(spec['body'])),1,0,v)

def parse_request(raw):
    require(len(raw)==128,'ordering request must contain sixteen words')
    packet=struct.unpack('<16Q',raw)
    c=packet[6]
    expected=request(packet[2],(c>>8)&255,(c&255)-1,(c>>16)&255,c>>32,packet[3])
    require(packet==expected,'ordering owner/context/source mismatch')
    return packet

def make_reply(packet,body):
    parse_request(struct.pack('<16Q',*packet))
    spec=CASES['fex_order_'+NAMES[packet[15]]]
    require(body==bytes.fromhex(spec['body']) and len(body)<=32,'ordering generated body mismatch')
    reply=list(packet)
    reply[0]=DELIVER
    reply[9:13]=struct.unpack('<4Q',body.ljust(32,b'\0'))
    return struct.pack('<16Q',*reply)

def compile_request(directory,producer):
    directory=Path(directory).resolve()
    packet=parse_request(bounded(directory/'request.bin',128))
    require(directory.parent.name==f'cpu-{packet[3]}' and directory.name=='request-000','ordering compilation owner directory differs')
    case='fex_order_'+NAMES[packet[15]]
    spec=CASES[case]
    with compiler_slot(directory.parent.parent):
        compile_source(directory/'compile',Path(producer),bytes.fromhex(spec['source']),packet[7],64,spec['instructions'],spec['host_count'])
    export_program(directory/'compile',case)
    program=verify(directory/'compile',dict(case=case,guest_rip=packet[7],bitness=64,source_hex=spec['source']))
    reply=make_reply(packet,bytes.fromhex(program['body_hex']))
    with (directory/'reply.bin').open('xb') as stream: stream.write(reply)
    record=dict(schema='natemu-i4v-ordering-worker-v1',request=packet,reply=struct.unpack('<16Q',reply),producer_sha256=program['producer_sha256'],body_sha256=program['body_sha256'])
    with (directory/'worker.json').open('x') as stream: json.dump(record,stream,indent=2);stream.write('\n')
    verify_request(directory,packet)

def verify_request(directory,expected):
    directory=Path(directory)
    packet=parse_request(bounded(directory/'request.bin',128))
    require(list(packet)==list(expected),'compilation differs from resident request')
    case='fex_order_'+NAMES[packet[15]]
    spec=CASES[case]
    program=verify(directory/'compile',dict(case=case,guest_rip=packet[7],bitness=64,source_hex=spec['source']))
    raw=make_reply(packet,bytes.fromhex(program['body_hex']))
    require(bounded(directory/'reply.bin',128)==raw,'reply differs from actual compilation')
    record=dict(schema='natemu-i4v-ordering-worker-v1',request=list(packet),reply=list(struct.unpack('<16Q',raw)),producer_sha256=program['producer_sha256'],body_sha256=program['body_sha256'])
    require(json.loads(bounded(directory/'worker.json',8192))==record,'worker record differs')
    return program

def verify_compilations(root,requests,replies):
    directories=sorted(Path(root).glob('request-*'))
    require(len(directories)==len(requests)==len(replies)==1,'one whole-block compile per owner required')
    verify_request(directories[0],requests[0])
    raw=bounded(directories[0]/'reply.bin',128)
    require(struct.pack('<16Q',*replies[0])==raw,'reported delivery differs')
    require(bounded(directories[0]/'reply-delivered.bin',128)==raw,'delivered reply differs')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request-dir',required=True,type=Path)
    parser.add_argument('--producer',required=True,type=Path)
    args=parser.parse_args()
    compile_request(args.request_dir,args.producer)

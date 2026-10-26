"""Admit five finite native bodies and bind their sites to actual FEX output."""
import hashlib
import json
from pathlib import Path
import re
import struct

ENTRY_GPRS = dict(zip(('rax','rbx','rcx','rdx','rsi','rdi','rbp','rsp',
                      'r8','r9','r10','r11','r12','r13','r14','r15'),
                     (4,6,7,5,10,11,9,8,12,13,14,15,16,17,19,29)))
CASES = {
    'fex_alias_xor64': dict(source='660fefc14801d1488b030f94c2f30f7f07', instructions=5, host_count=11,
        body='101e316efb0005cafa0005abe7031aaac4fcdfc81f2003d5f4179f9a851e40b3bf3b03d57001803d1f4000d5',
        object_bytes=160, code_bytes=108, pc=[(0,4),(7,28),(13,44)],
        sites=[(16,7,8,False,6,0),(36,13,16,True,11,0)],
        flags='ADD updates flags; SETZ observes ZF; final CFINV'),
    'fex_alias_or64': dict(source='660febc14801d1488b030f94c2f30f7f07', instructions=5, host_count=11,
        body='101eb14efb0005cafa0005abe7031aaac4fcdfc81f2003d5f4179f9a851e40b3bf3b03d57001803d1f4000d5',
        object_bytes=160, code_bytes=108, pc=[(0,4),(7,28),(13,44)],
        sites=[(16,7,8,False,6,0),(36,13,16,True,11,0)],
        flags='ADD updates flags; SETZ observes ZF; final CFINV'),
    'fex_memory_data64': dict(source='488b03', instructions=1, host_count=2,
        body='c4fcdfc81f2003d5', object_bytes=128, code_bytes=76,
        pc=[(0,4),(0,12)], sites=[(0,0,8,False,6,0)]),
    'fex_push_immediates64': dict(source='68bbbcbdbe6668525166686261488b42f88b5af4', instructions=5, host_count=13,
        body='94688892b4d7b7f2148d1ff8542a8a5214ed1f78542c8c5214ed1f78b42000d184fedfc81f2003d5b43000d186fedf881f2003d5',
        object_bytes=176, code_bytes=116, pc=[(0,4),(0,12),(5,24),(9,32),(13,40),(17,52)],
        sites=[(8,0,8,True,8,-8),(16,5,2,True,8,-2),(24,9,2,True,8,-2),
               (32,13,8,False,20,0),(44,17,4,False,20,0)]),
    'fex_push_immediates64_changed': dict(source='68bbbcbdbe6668535166686261488b42f88b5af4', instructions=5, host_count=13,
        body='94688892b4d7b7f2148d1ff8742a8a5214ed1f78542c8c5214ed1f78b42000d184fedfc81f2003d5b43000d186fedf881f2003d5',
        object_bytes=176, code_bytes=116, pc=[(0,4),(0,12),(5,24),(9,32),(13,40),(17,52)],
        sites=[(8,0,8,True,8,-8),(16,5,2,True,8,-2),(24,9,2,True,8,-2),
               (32,13,8,False,20,0),(44,17,4,False,20,0)]),
}

def require(condition, message):
    if not condition: raise ValueError(message)

def sha(data): return hashlib.sha256(data).hexdigest()

def bounded(path, size):
    with path.open('rb') as stream: data=stream.read(size+1)
    require(len(data)<=size, f'oversized artifact: {path.name}')
    return data

def parse(directory, case):
    directory=Path(directory)
    require(case in CASES,'unsupported finite case')
    spec=CASES[case]
    record=json.loads(bounded(directory/'compiler.json',256*1024))
    pin=json.loads(Path(__file__).with_name('producer.json').read_text())
    require(record['status']=='exported_not_executed' and record['instruction_count_check']==0,'incomplete or count-mismatched compile')
    require(record['producer_sha256']==pin['binary_sha256'] and record['image']==pin['image'],'wrong producer/image')
    rip=record['guest_rip']; source=bytes.fromhex(spec['source'])
    require(record['bitness']==64 and record['source_hex']==source.hex() and record['instructions']==spec['instructions'], 'case/source/mode mismatch')
    require(record['expected_host']==spec['host_count'], 'wrong expected host count')
    require(0 <= rip <= 2**64-1-len(source) and rip>>47 in (0,0x1ffff),'noncanonical source window')
    require(bounded(directory/'source.x86',64)==source, 'source artifact mismatch')
    expected=struct.pack('<6Q',64,25,1,8192,49155,0)
    expected+=struct.pack('<128sqQQI',b'natemu-i4v-request',spec['host_count'],len(source),spec['instructions'],0x41424344)+source
    require(bounded(directory/'input.bin',268)==expected,'packed FEX input mismatch')
    log=bounded(directory/'producer.log',128*1024).decode()
    require(f'NATEMU_WINDOW index=1 guest_rip={rip} source_address=65536 bytes={len(source)} instructions={spec["instructions"]}' in log,'source/guest address binding absent')
    require(f'NATEMU_ENTRY index=1 guest_rip={rip}' in log and 'NATEMU_MODE bitness=64' in log, 'actual compilation entry/mode mismatch')
    require('NATEMU_COMPLETED tests=1 result=0' in log and 'NATEMU_FEATURES sve128=0 sve256=0 rcpc=0 imm9=0' in log, 'incomplete/wrong-feature compilation')
    objects=[json.loads(line[len('NATEMU_OBJECT '):]) for line in log.splitlines() if line.startswith('NATEMU_OBJECT ')]
    require(len(objects)==2 and [o['index'] for o in objects]==[0,1], 'unexpected object count')
    obj=objects[1]
    require((obj['bytes'],obj['code_bytes'],obj['code_offset'],obj['guest_instructions'],obj['relocation_bytes'])==
            (spec['object_bytes'],spec['code_bytes'],4,spec['instructions'],144),'unexpected native object layout')
    require(record['object']==obj,'compiler record/object mismatch')
    raw=bounded(directory/'outputs/001.native',65536)
    reloc=bounded(directory/'outputs/001.relocations',65536)
    require(len(raw)==obj['bytes'] and len(reloc)==obj['relocation_bytes'],'truncated object/relocations')
    pc=[tuple(map(int,m)) for m in re.findall(r'^NATEMU_GUEST_PC index=1 guest_offset=(\d+) native_offset=(\d+)$',log,re.M)]
    require(pc==spec['pc'],'unexpected guest/native PC association')
    body=bytes.fromhex(spec['body']);end=12+len(body)
    require(raw[4:12].hex()=='e0ffff10800300f9' and raw[end:end+4].hex()=='0f420000','unexpected body boundaries')
    require(raw[12:end]==body,'native body differs from admitted emitted instructions')
    commands=record['commands']
    require(len(commands)==4 and all(c['returncode']==0 for c in commands),'incomplete compiler lifecycle')
    require([c['argv'][:2] for c in commands]==[['docker','create'],['docker','start'],['docker','inspect'],['docker','rm']], 'wrong compiler lifecycle')
    argv=commands[0]['argv']
    require(all(v in argv for v in ('--cpus=2','--memory=512m','--memory-swap=512m','--pids-limit=128','--network=none','--read-only',pin['image'],f'NATEMU_GUEST_RIP={rip}')), 'unbounded/wrong compilation invocation')
    sites=[]
    for native,guest,width,write,reg,disp in spec['sites']:
        sites.append(dict(native_offset=native,guest_offset=guest,guest_rip=rip+guest,width=width,
                          write=write,address_register=reg,address_displacement=disp,
                          continuation=native+4,phase='precommit',guest_rsp_register=8))
    return dict(schema='natemu-i4v-program-v1',case=case,guest_rip=rip,bitness=64,
                source_hex=source.hex(),source_sha256=sha(source),body_hex=body.hex(),body_sha256=sha(body),
                producer_sha256=record['producer_sha256'],object=obj,native_object_sha256=sha(raw),
                relocations_sha256=sha(reloc),extraction=[12,end],guest_pc=[dict(guest_offset=g,native_offset=n) for g,n in pc],
                entry_gprs=ENTRY_GPRS,flags=spec.get('flags','unchanged'),sites=sites)

def export_program(directory, case):
    directory=Path(directory); result=parse(directory,case)
    (directory/'body.bin').write_bytes(bytes.fromhex(result['body_hex']))
    (directory/'export.json').write_text(json.dumps(result,indent=2)+'\n')
    return result

def verify(directory, expected=None):
    directory=Path(directory)
    retained=json.loads(bounded(directory/'export.json',32768))
    actual=parse(directory,retained['case'])
    require(actual==retained,'program record differs from producing artifacts')
    require(bounded(directory/'body.bin',512)==bytes.fromhex(actual['body_hex']),'delivered body differs from fresh FEX body')
    if expected:
        require(all(actual.get(k)==v for k,v in expected.items()), 'program differs from requested identity')
    return actual

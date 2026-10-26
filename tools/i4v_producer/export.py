"""Admit finite native bodies and bind their sites to actual FEX output."""
import hashlib
import json
from pathlib import Path
import re
import struct

ENTRY_GPRS = dict(zip(('rax','rbx','rcx','rdx','rsi','rdi','rbp','rsp',
                      'r8','r9','r10','r11','r12','r13','r14','r15'),
                     (4,6,7,5,10,11,9,8,12,13,14,15,16,17,19,29)))
CASES = {
    'fex_lfence_dependencies': dict(bitness=64, source='0f58c14801d10faee89f0f90c048890f66894708f30f7f4710', instructions=8, host_count=30,
        body='10d6314efb0005cafa0005abe7031aaabf3d03d5f4379f9a75031acab5120453941215aa40135a4a0008404a1504404ab5fa7fb294faf5aa15423bd5b5066292946255aa94027fb2841e78b3f4779f9a841e40b31f2003d567fd9fc8742100911f2003d584fe9f4814028052bf3b03d570e9b43c1f4000d5', object_bytes=240, code_bytes=188,
        pc=[(0,4),(13,96),(16,104),(20,116)], sites=[(88,13,8,True,11,0),(100,16,2,True,20,0),(112,20,16,True,11,16)],
        flags='add_rcx_rdx', native_writes=[0,4,7,20,21,26,27], barriers=[(16,6,'dmb ld'),(108,20,'dmb ish')],
        fence_roots=dict(native_offset=16, guest_offset=6, rcx=7, pf_result=26, af_inputs_xor=27, xmm0=16, status=['NZCV','FPSR'], native_carry='x86_cf_before_final_cfinv')),
    'fex_order_sb': dict(bitness=64, source='c703010000008b02', instructions=2, host_count=5,
        body='340080521f2003d5d4fc9f88a4fcdf881f2003d5', object_bytes=144, code_bytes=84,
        pc=[(0, 4), (0, 12), (6, 24)], sites=[(8, 0, 4, True, 6, 0), (12, 6, 4, False, 5, 0)],
        site_effects={8: {'kind': 'store', 'committed_guest_bytes': 0}, 12: {'kind': 'load', 'committed_guest_bytes': 6}}, native_writes=[4, 20], barriers=[]),
    'fex_order_mp_writer': dict(bitness=64, source='c70301000000c70201000000', instructions=2, host_count=5,
        body='340080521f2003d5d4fc9f881f2003d5b4fc9f88', object_bytes=144, code_bytes=84,
        pc=[(0, 4), (0, 12), (6, 24)], sites=[(8, 0, 4, True, 6, 0), (16, 6, 4, True, 5, 0)],
        site_effects={8: {'kind': 'store', 'committed_guest_bytes': 0}, 16: {'kind': 'store', 'committed_guest_bytes': 6}}, native_writes=[20], barriers=[]),
    'fex_order_reader': dict(bitness=64, source='8b038b0a', instructions=2, host_count=4,
        body='c4fcdf881f2003d5a7fcdf881f2003d5', object_bytes=144, code_bytes=84,
        pc=[(0, 4), (0, 12), (2, 20)], sites=[(0, 0, 4, False, 6, 0), (8, 2, 4, False, 5, 0)],
        site_effects={0: {'kind': 'load', 'committed_guest_bytes': 0}, 8: {'kind': 'load', 'committed_guest_bytes': 2}}, native_writes=[4, 7], barriers=[]),
    'fex_order_wrc_middle': dict(bitness=64, source='8b03c70201000000', instructions=2, host_count=5,
        body='c4fcdf881f2003d5340080521f2003d5b4fc9f88', object_bytes=144, code_bytes=84,
        pc=[(0, 4), (0, 12), (2, 20)], sites=[(0, 0, 4, False, 6, 0), (16, 2, 4, True, 5, 0)],
        site_effects={0: {'kind': 'load', 'committed_guest_bytes': 0}, 16: {'kind': 'store', 'committed_guest_bytes': 2}}, native_writes=[4, 20], barriers=[]),
    'fex_order_writer': dict(bitness=64, source='c70301000000', instructions=1, host_count=3,
        body='340080521f2003d5d4fc9f88', object_bytes=128, code_bytes=76,
        pc=[(0, 4), (0, 12)], sites=[(8, 0, 4, True, 6, 0)],
        site_effects={8: {'kind': 'store', 'committed_guest_bytes': 0}}, native_writes=[20], barriers=[]),
    'fex_order_sb_mfence': dict(bitness=64, source='c703010000000faef08b02', instructions=3, host_count=6,
        body='340080521f2003d5d4fc9f88bf3f03d5a4fcdf881f2003d5', object_bytes=144, code_bytes=92,
        pc=[(0, 4), (0, 12), (9, 28)], sites=[(8, 0, 4, True, 6, 0), (16, 9, 4, False, 5, 0)],
        site_effects={8: {'kind': 'store', 'committed_guest_bytes': 0}, 16: {'kind': 'load', 'committed_guest_bytes': 9}}, native_writes=[4, 20], barriers=[(12, 6, 'dmb sy')]),
    'fex_order_sb_xchg': dict(bitness=64, source='c7030100000087068b02', instructions=3, host_count=6,
        body='340080521f2003d5d4fc9f884481e4b8a4fcdf881f2003d5', object_bytes=144, code_bytes=92,
        pc=[(0, 4), (0, 12), (6, 24), (8, 28)], sites=[(8, 0, 4, True, 6, 0), (12, 6, 4, True, 10, 0), (16, 8, 4, False, 5, 0)],
        site_effects={8: {'kind': 'store', 'committed_guest_bytes': 0}, 12: {'kind': 'atomic_exchange', 'committed_guest_bytes': 6, 'read': True, 'write': True, 'result_register': 4}, 16: {'kind': 'load', 'committed_guest_bytes': 8}}, native_writes=[4, 20], barriers=[]),
    'fex_order_xchg_result': dict(bitness=64, source='87038902', instructions=2, host_count=3,
        body='c480e4b81f2003d5a4fc9f88', object_bytes=128, code_bytes=76,
        pc=[(0, 4), (0, 12), (2, 16)], sites=[(0, 0, 4, True, 6, 0), (8, 2, 4, True, 5, 0)],
        site_effects={0: {'kind': 'atomic_exchange', 'committed_guest_bytes': 0, 'read': True, 'write': True, 'result_register': 4}, 8: {'kind': 'store', 'committed_guest_bytes': 2}}, native_writes=[4], barriers=[]),
    'fex_order_lfence': dict(bitness=64, source='0faee8', instructions=1, host_count=1,
        body='bf3d03d5', object_bytes=128, code_bytes=68,
        pc=[(0, 4)], sites=[],
        site_effects={}, native_writes=[], barriers=[(0, 0, 'dmb ld')]),
    'fex_order_sfence': dict(bitness=64, source='0faef8', instructions=1, host_count=1,
        body='bf3e03d5', object_bytes=128, code_bytes=68,
        pc=[(0, 4)], sites=[],
        site_effects={}, native_writes=[], barriers=[(0, 0, 'dmb st')]),
    'fex_order_mfence': dict(bitness=64, source='0faef0', instructions=1, host_count=1,
        body='bf3f03d5', object_bytes=128, code_bytes=68,
        pc=[(0, 4)], sites=[],
        site_effects={}, native_writes=[], barriers=[(0, 0, 'dmb sy')]),
    'fex_order_xchg': dict(bitness=64, source='8703', instructions=1, host_count=1,
        body='c480e4b8', object_bytes=128, code_bytes=68,
        pc=[(0, 4), (0, 12)], sites=[(0, 0, 4, True, 6, 0)],
        site_effects={0: {'kind': 'atomic_exchange', 'committed_guest_bytes': 0, 'read': True, 'write': True, 'result_register': 4}}, native_writes=[4], barriers=[]),
    'fex_address_store_ecx': dict(bitness=32, source='890b', instructions=1, host_count=2,
        body='1f2003d5c7fc9f88', object_bytes=128, code_bytes=76,
        pc=[(0,4),(0,12)], sites=[(4,0,4,True,6,0)]),
    'fex_address_store_edx': dict(bitness=32, source='8913', instructions=1, host_count=2,
        body='1f2003d5c5fc9f88', object_bytes=128, code_bytes=76,
        pc=[(0,4),(0,12)], sites=[(4,0,4,True,6,0)]),
    'fex_address_load_edx': dict(bitness=32, source='8b02', instructions=1, host_count=2,
        body='a4fcdf881f2003d5', object_bytes=128, code_bytes=76,
        pc=[(0,4),(0,12)], sites=[(0,0,4,False,5,0)]),
    'fex_address_store_ecx16': dict(bitness=32, source='678908', instructions=1, host_count=4,
        body='d4000a0b943e00531f2003d587fe9f88', object_bytes=144, code_bytes=84,
        pc=[(0,4),(0,12)], sites=[(12,0,4,True,20,0)]),
    'fex_address_store_edx16': dict(bitness=32, source='678910', instructions=1, host_count=4,
        body='d4000a0b943e00531f2003d585fe9f88', object_bytes=144, code_bytes=84,
        pc=[(0,4),(0,12)], sites=[(12,0,4,True,20,0)]),
    'fex_address_load16': dict(bitness=32, source='678b00', instructions=1, host_count=4,
        body='d4000a0b943e005384fedf881f2003d5', object_bytes=144, code_bytes=84,
        pc=[(0,4),(0,12)], sites=[(8,0,4,False,20,0)]),
    'fex_mov_eax_00000100': dict(bitness=32, source='b800010000', instructions=1, host_count=1,
        body='04208052', object_bytes=128, code_bytes=68, pc=[(0,4)], sites=[]),
    'fex_mov_eax_00006000': dict(bitness=32, source='b800600000', instructions=1, host_count=1,
        body='04008c52', object_bytes=128, code_bytes=68, pc=[(0,4)], sites=[]),
    'fex_mov_eax_00010011_mode64': dict(bitness=64, source='b811000100', instructions=1, host_count=2,
        body='240280522400a072', object_bytes=128, code_bytes=76, pc=[(0,4)], sites=[]),
    'fex_mov_eax_00000620': dict(bitness=32, source='b820060000', instructions=1, host_count=1,
        body='04c48052', object_bytes=128, code_bytes=68, pc=[(0,4)], sites=[]),
    'fex_mov_eax_000006a0': dict(bitness=32, source='b8a0060000', instructions=1, host_count=1,
        body='04d48052', object_bytes=128, code_bytes=68, pc=[(0,4)], sites=[]),
    'fex_mov_eax_00001020': dict(bitness=32, source='b820100000', instructions=1, host_count=1,
        body='04048252', object_bytes=128, code_bytes=68, pc=[(0,4)], sites=[]),
    'fex_mov_eax_00001003': dict(bitness=32, source='b803100000', instructions=1, host_count=1,
        body='64008252', object_bytes=128, code_bytes=68, pc=[(0,4)], sites=[]),
    'fex_mov_eax_00000011': dict(bitness=32, source='b811000000', instructions=1, host_count=1,
        body='24028052', object_bytes=128, code_bytes=68, pc=[(0,4)], sites=[]),
    'fex_mov_eax_80000011': dict(bitness=32, source='b811000080', instructions=1, host_count=2,
        body='240280520400b072', object_bytes=128, code_bytes=76, pc=[(0,4)], sites=[]),
    'fex_mov_eax_00000680': dict(bitness=32, source='b880060000', instructions=1, host_count=1,
        body='04d08052', object_bytes=128, code_bytes=68, pc=[(0,4)], sites=[]),
    'fex_mov_eax_00010011': dict(bitness=32, source='b811000100', instructions=1, host_count=2,
        body='240280522400a072', object_bytes=128, code_bytes=76,
        pc=[(0, 4)], sites=[]),
    'fex_mov_eax_80010011': dict(bitness=32, source='b811000180', instructions=1, host_count=2,
        body='240280522400b072', object_bytes=128, code_bytes=76,
        pc=[(0, 4)], sites=[]),
    'fex_mov_eax_00001000': dict(bitness=32, source='b800100000', instructions=1, host_count=1,
        body='04008252', object_bytes=128, code_bytes=68,
        pc=[(0, 4)], sites=[]),
    'fex_mov_eax_00003000': dict(bitness=32, source='b800300000', instructions=1, host_count=1,
        body='04008652', object_bytes=128, code_bytes=68,
        pc=[(0, 4)], sites=[]),
    'fex_mov_eax_00000000': dict(bitness=32, source='b800000000', instructions=1, host_count=1,
        body='04008052', object_bytes=128, code_bytes=68,
        pc=[(0, 4)], sites=[]),
    'fex_mov_eax_80010010': dict(bitness=32, source='b810000180', instructions=1, host_count=2,
        body='040280522400b072', object_bytes=128, code_bytes=76,
        pc=[(0, 4)], sites=[]),
    'fex_mov_eax_20010011': dict(bitness=32, source='b811000120', instructions=1, host_count=2,
        body='240280522400a472', object_bytes=128, code_bytes=76,
        pc=[(0, 4)], sites=[]),
    'fex_mov_eax_ffffffff': dict(bitness=32, source='b8ffffffff', instructions=1, host_count=1,
        body='04008012', object_bytes=128, code_bytes=68,
        pc=[(0, 4)], sites=[]),
    'fex_mov_eax_00000600': dict(bitness=32, source='b800060000', instructions=1, host_count=1,
        body='04c08052', object_bytes=128, code_bytes=68,
        pc=[(0, 4)], sites=[]),
    'fex_store_eax_rbx64': dict(bitness=64, source='8903', instructions=1, host_count=2,
        body='1f2003d5c4fc9f88', object_bytes=128, code_bytes=76,
        pc=[(0, 4), (0, 12)], sites=[(4, 0, 4, True, 6, 0)]),
    'fex_load_eax_rbx64': dict(bitness=64, source='8b03', instructions=1, host_count=2,
        body='c4fcdf881f2003d5', object_bytes=128, code_bytes=76,
        pc=[(0, 4), (0, 12)], sites=[(0, 0, 4, False, 6, 0)]),
    'fex_store_eax_ebx32': dict(bitness=32, source='8903', instructions=1, host_count=2,
        body='1f2003d5c4fc9f88', object_bytes=128, code_bytes=76,
        pc=[(0, 4), (0, 12)], sites=[(4, 0, 4, True, 6, 0)]),
    'fex_load_eax_ebx32': dict(bitness=32, source='8b03', instructions=1, host_count=2,
        body='c4fcdf881f2003d5', object_bytes=128, code_bytes=76,
        pc=[(0, 4), (0, 12)], sites=[(0, 0, 4, False, 6, 0)]),
    'fex_mixed32_old': dict(bitness=32, source='89c1890b8b02', instructions=3, host_count=5,
        body='e70304aa1f2003d5c7fc9f88a4fcdf881f2003d5', object_bytes=144, code_bytes=84,
        pc=[(0,4),(2,16),(4,24)], sites=[(8,2,4,True,6,0),(12,4,4,False,5,0)]),
    'fex_mixed32_changed': dict(bitness=32, source='89c189138b02', instructions=3, host_count=5,
        body='e70304aa1f2003d5c5fc9f88a4fcdf881f2003d5', object_bytes=144, code_bytes=84,
        pc=[(0,4),(2,16),(4,24)], sites=[(8,2,4,True,6,0),(12,4,4,False,5,0)]),
    'fex_moffs_load32_step0': dict(bitness=32, source='ba00001000', instructions=1, host_count=1,
        body='0502a052', object_bytes=128, code_bytes=68,
        pc=[(0, 4)], sites=[]),
    'fex_moffs_load32_step1': dict(bitness=32, source='b842000000', instructions=1, host_count=1,
        body='44088052', object_bytes=128, code_bytes=68,
        pc=[(0, 4)], sites=[]),
    'fex_moffs_load32_step2': dict(bitness=32, source='8902', instructions=1, host_count=2,
        body='1f2003d5a4fc9f88', object_bytes=128, code_bytes=76,
        pc=[(0, 4), (0, 12)], sites=[(4, 0, 4, True, 5, 0)]),
    'fex_moffs_load32_step3': dict(bitness=32, source='b8ffffffff', instructions=1, host_count=1,
        body='04008012', object_bytes=128, code_bytes=68,
        pc=[(0, 4)], sites=[]),
    'fex_moffs_load32_step4': dict(bitness=32, source='a100001000', instructions=1, host_count=3,
        body='1402a05284fedf881f2003d5', object_bytes=128, code_bytes=76,
        pc=[(0, 4)], sites=[(4, 0, 4, False, 20, 0)]),
    'fex_moffs_load32_step5': dict(bitness=32, source='89c2', instructions=1, host_count=1,
        body='e50304aa', object_bytes=128, code_bytes=68,
        pc=[(0, 4)], sites=[]),
    'fex_moffs_load32_step7': dict(bitness=32, source='66a100001000', instructions=1, host_count=4,
        body='1402a05294fedf481f2003d5843e40b3', object_bytes=144, code_bytes=84,
        pc=[(0, 4)], sites=[(4, 0, 2, False, 20, 0)]),
    'fex_moffs_load32_step8': dict(bitness=32, source='89c1', instructions=1, host_count=1,
        body='e70304aa', object_bytes=128, code_bytes=68,
        pc=[(0, 4)], sites=[]),
    'fex_moffs_load32': dict(bitness=32,
        source='ba00001000b8420000008902b8ffffffffa10000100089c2b8ffffffff66a10000100089c1',
        instructions=9, host_count=16,
        body='0502a05244088052f40305aa1f2003d5a4fc9f880400801295fedf881f2003d5f60304aae40315aae50304aae40316aa94fedf481f2003d5843e40b3e70304aa',
        object_bytes=192, code_bytes=132, pc=[(0,4),(10,20)],
        # Exact retained instruction sequences identify these memory sites; the
        # sparse FEX PC table does not provide nine instruction boundaries.
        sites=[(16,10,4,True,5,0),(24,17,4,False,20,0),(48,29,2,False,20,0)]),
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
    spec=CASES[case]; bitness=spec.get('bitness',64)
    record=json.loads(bounded(directory/'compiler.json',256*1024))
    pin=json.loads(Path(__file__).with_name('producer.json').read_text())
    require(record['status']=='exported_not_executed' and record['instruction_count_check']==0,'incomplete or count-mismatched compile')
    require(record['producer_sha256']==pin['binary_sha256'] and record['image']==pin['image'],'wrong producer/image')
    rip=record['guest_rip']; source=bytes.fromhex(spec['source'])
    require(record['bitness']==bitness and record['source_hex']==source.hex() and record['instructions']==spec['instructions'], 'case/source/mode mismatch')
    require(record['expected_host']==spec['host_count'], 'wrong expected host count')
    require(0 <= rip <= 2**bitness-1-len(source) and (bitness==32 or rip>>47 in (0,0x1ffff)),'invalid source address window')
    require(bounded(directory/'source.x86',64)==source, 'source artifact mismatch')
    expected=struct.pack('<6Q',bitness,25,1,8192,49155,0)
    expected+=struct.pack('<128sqQQI',b'natemu-i4v-request',spec['host_count'],len(source),spec['instructions'],0x41424344)+source
    require(bounded(directory/'input.bin',268)==expected,'packed FEX input mismatch')
    log=bounded(directory/'producer.log',128*1024).decode()
    require(f'NATEMU_WINDOW index=1 guest_rip={rip} source_address=65536 bytes={len(source)} instructions={spec["instructions"]}' in log,'source/guest address binding absent')
    require(f'NATEMU_ENTRY index=1 guest_rip={rip}' in log and f'NATEMU_MODE bitness={bitness}' in log, 'actual compilation entry/mode mismatch')
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
        sites[-1].update(spec.get('site_effects',{}).get(native,{}))
    result = dict(schema='natemu-i4v-program-v1',case=case,guest_rip=rip,bitness=bitness,
                source_hex=source.hex(),source_sha256=sha(source),body_hex=body.hex(),body_sha256=sha(body),
                producer_sha256=record['producer_sha256'],object=obj,native_object_sha256=sha(raw),
                relocations_sha256=sha(reloc),extraction=[12,end],guest_pc=[dict(guest_offset=g,native_offset=n) for g,n in pc],
                entry_gprs=ENTRY_GPRS,flags=spec.get('flags','unchanged'),sites=sites)
    for field in ('native_writes','barriers','fence_roots'):
        if field in spec: result[field]=json.loads(json.dumps(spec[field]))
    return result

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

#!/usr/bin/env python3
"""Repeated real block reset/disable with a HLT peer and two split-atomic peers.

One N4 VM per case, 64 generations, 120s cap; no automatic retries. Scheduling
is testing-only; normal production coordinator/resident run the requests.
"""
import argparse
import copy
import fcntl
import json
import re
from pathlib import Path
import struct
import subprocess
import tempfile
import time

import run_virtio_lifecycle as life
import run_io_windows as windows

ROOT = windows.ROOT
BASE, TEMPLATE, EXPECT, FLAGS = life.BASE, 0x800000, 0x900000, 0xa00000
ROUNDS = 64


def save(p, obj): p.write_text(json.dumps(obj, indent=2) + '\n')
def num(v): return int(v, 0) if isinstance(v, str) else v


def fixture(case, work, corrupt_cycle=0):
    meta = life.fixture(case, work)
    original = (work / 'program.txt').read_text()
    initial = bytes.fromhex(next(line.split()[2] for line in original.splitlines() if line.startswith(f'memory {BASE:#x} ')))
    asm = (work / 'program.asm').read_text().removeprefix('.intel_syntax noprefix\n')
    tail = 'xor r15d, r15d\njmp finished\nfailed:\nnop\nfinished:\nnop\n'
    assert asm.endswith(tail)
    body = asm[:-len(tail)]
    disk = (work / 'disk.raw').read_bytes()
    expected_reads = disk[512:512 * (ROUNDS + 1)]
    # Optional isolated guest corruption validates that a non-final bad generation cannot hide.
    corrupt = f'cmp r13, {corrupt_cycle - 1}\njne checked_generation\nmov byte ptr [{BASE + 0x15000:#x}], 1\nchecked_generation:\n' if corrupt_cycle else ''
    check = corrupt + f'''cmp qword ptr [{BASE + 0x2004:#x}], 0
jne failed
cmp byte ptr [{BASE + 0x15000:#x}], 0
jne failed
cmp word ptr [{BASE + 0x12002:#x}], 1
jne failed
cmp dword ptr [{BASE + 0x12004:#x}], 0
jne failed
cmp dword ptr [{BASE + 0x12008:#x}], 513
jne failed
mov esi, {BASE + 0x6000:#x}
mov edi, {TEMPLATE + 0x6000:#x}
mov ecx, 64
repe cmpsq
jne failed
mov esi, {BASE + 0x16000:#x}
mov rdi, r13
shl rdi, 9
add rdi, {EXPECT:#x}
mov ecx, 64
repe cmpsq
jne failed
mov dword ptr [r12 + 0x44], 0
mov eax, dword ptr [r12 + 0x44]
test eax, eax
jne failed
inc r13
cmp r13, {ROUNDS}
jb cycle
mov qword ptr [{FLAGS:#x}], 1
xor r15d, r15d
jmp finished
failed:
mov r15, 0xbad
mov qword ptr [{FLAGS:#x}], 1
finished:
nop
'''
    prefix = f'''xor r13d, r13d
ready:
cmp qword ptr [{FLAGS + 8:#x}], 1
jne ready
cmp qword ptr [{FLAGS + 16:#x}], 1
jne ready
cmp qword ptr [{FLAGS + 24:#x}], 1
jne ready
cycle:
mov esi, {TEMPLATE:#x}
mov edi, {BASE:#x}
mov ecx, {len(initial) // 8}
rep movsq
mov rax, r13
inc rax
mov qword ptr [{BASE + 0x13008:#x}], rax
'''
    main = prefix + body + check
    code = life.ref.assemble(main)
    flags = bytearray(4096)
    for v in (2, 3): struct.pack_into('<Q', flags, 0x7c + (v - 2) * 64, 0x5555555555555555)
    regions = {BASE: initial, TEMPLATE: initial, EXPECT: expected_reads, FLAGS: bytes(flags)}
    lines = ['identity', f'mmio {life.MMIO:#x} {life.MMIO:#x} unassigned', f'body {life.CODE:#x} {code.hex()}']
    lines += [f'memory {at:#x} {b.hex()}' for at, b in regions.items()]
    ends = [life.CODE + len(code)]
    for v in range(1, 4):
        if v == 1:
            asm = f'mov qword ptr [{FLAGS + 8:#x}], 1\ncli\nhlt\nafter_hlt:\nmov r15, 0xbad\nnop\n'
        else:
            asm = f'''xor r14d, r14d
mov qword ptr [{FLAGS + v * 8:#x}], 1
again:
lock not qword ptr [{FLAGS + 0x7c + (v - 2) * 64:#x}]
inc r14
cmp qword ptr [{FLAGS:#x}], 0
je again
mov qword ptr [{FLAGS + 32 + (v - 2) * 8:#x}], r14
nop
'''
        peer, labels = life.ref.assemble(asm, labels=True)
        start = life.CODE + v * 0x10000
        ends.append(start + (labels['after_hlt'] if v == 1 else len(peer)))
        lines.append(f'body {start:#x} {peer.hex()}')
        (work / f'cpu-{v}.asm').write_text('.intel_syntax noprefix\n' + asm)
    for v in range(4): lines += [f'region {life.STACK + v * 0x1000:#x} 0x1000']
    for v in range(4):
        regs = [0] * 16; regs[4] = life.STACK + v * 0x1000 + 0xff0
        lines += ['cpu', f'rip {life.CODE + v * 0x10000:#x}', 'gpr ' + ' '.join(map(hex, regs)), 'cpl 0', 'rflags 0x2', 'if 0']
    (work / 'program.txt').write_text('\n'.join(lines) + '\n')
    (work / 'program.asm').write_text('.intel_syntax noprefix\n' + main)
    expected = bytearray((work / 'expected-memory.bin').read_bytes())
    struct.pack_into('<Q', expected, 0x13008, ROUNDS)
    expected[0x16000:0x16200] = disk[ROUNDS * 512:(ROUNDS + 1) * 512]
    (work / 'expected-memory.bin').write_bytes(expected)
    meta.update(n=4, rounds=ROUNDS, ends=ends, mode='cycle-' + case, corrupt_cycle=corrupt_cycle, regions={str(k):v.hex() for k,v in regions.items()})
    save(work / 'oracle.json', meta)
    return meta


def judge(meta, report, states, trace, memories, disk, expected_disk, expected_memory):
    errors = []
    def need(ok, message):
        if not ok: errors.append(message)
    cpus = report['cpus']; need(len(cpus) == len(states) == len(memories) == 4, 'four complete participants')
    if len(cpus) != 4 or len(states) != 4 or len(memories) != 4: return errors
    need(disk == expected_disk, 'read-only disk effects')
    need(report['io_disk'] == trace, 'trace consistency')
    need(not trace['entered'] and trace['calls'] == 2 * ROUNDS, 'exact backend attempts/retirement')
    events = trace['events']
    need([e for e in events if e.startswith('read-result:')] == ['read-result:Ok(512)'] * (2 * ROUNDS), 'every native result exact')
    for e, count in [('backend-held', ROUNDS), ('backend-released', ROUNDS), ('commit', ROUNDS), ('barrier-timeout', 0), ('trace-overflow', 0)]: need(events.count(e) == count, e + ' count')
    sequence = []
    for i in range(ROUNDS): sequence += ['backend-held', meta['case'] + '-deferred', 'backend-released', 'read:0:512', 'read-result:Ok(512)', f'read:{512 * (i + 1)}:512', 'read-result:Ok(512)', 'commit']
    need(life.ordered(events, sequence), 'every generation canceled then recovered in order')
    regions = {int(k):bytes.fromhex(v) for k,v in meta['regions'].items()}; regions[BASE] = expected_memory
    flags = bytearray(regions[FLAGS]); struct.pack_into('<QQQQ', flags, 0, 1, 1, 1, 1)
    for v in range(4):
        cpu = cpus[v]; gpr = list(map(num, cpu['gpr']))
        need(num(cpu['rip']) == meta['ends'][v] and num(states[v]['rip']) == meta['ends'][v], f'CPU{v} end RIP')
        need(list(map(num, states[v]['gpr'])) == gpr, f'CPU{v} final register reports agree')
        need(gpr[15] == 0 and not cpu['tampered'] and cpu['frame_intact'], f'CPU{v} assertions/frame')
        need(cpu['stop'] == ('resident' if v == 1 else 'reached'), f'CPU{v} terminal stop')
        if v == 0: need(gpr[13] == ROUNDS, 'all generations guest-verified')
        if v == 1: need('halted' in cpu['reason'] and cpu['stats']['halts'] > 0, 'peer remained in HLT')
        if v >= 2:
            need(gpr[14] > 0 and cpu['stats']['splits'] > 0, f'CPU{v} split progress')
            struct.pack_into('<Q', flags, 32 + (v - 2) * 8, gpr[14])
            struct.pack_into('<Q', flags, 0x7c + (v - 2) * 64, 0xaaaaaaaaaaaaaaaa if gpr[14] % 2 else 0x5555555555555555)
    regions[FLAGS] = bytes(flags)
    for v in range(4):
        for label, entries in [('postjoin', memories[v]), ('report', cpus[v]['memory'])]:
            observed = {num(m['addr']):bytes.fromhex(m['data']) for m in entries}
            need(len(entries) == len(regions) and observed == regions, f'CPU{v} {label} whole RAM oracle')
    return errors


def self_check():
    controls = 0
    for case in ('reset', 'disable'):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d); meta = fixture(case, p)
            assert all(line.startswith('mov ') for line in (p/'program.asm').read_text().splitlines() if '[r12' in line), 'MMIO requires supported MOV access'
            regions = {int(k):bytes.fromhex(v) for k,v in meta['regions'].items()}; regions[BASE] = (p/'expected-memory.bin').read_bytes()
            flags = bytearray(regions[FLAGS]); struct.pack_into('<QQQQQQ', flags, 0, 1, 1, 1, 1, 2, 2); regions[FLAGS] = bytes(flags)
            memory = [{'addr':hex(k),'data':v.hex()} for k,v in regions.items()]
            events=[]
            for i in range(ROUNDS): events += ['backend-held', case+'-deferred', 'backend-released', 'read:0:512', 'read-result:Ok(512)', f'read:{512*(i+1)}:512', 'read-result:Ok(512)', 'commit']
            trace={'entered':False,'calls':ROUNDS*2,'events':events};cpus=[]
            for v in range(4):
                gpr=[0]*16;gpr[13]=ROUNDS if v==0 else 0;gpr[14]=2 if v>=2 else 0
                cpus.append({'rip':hex(meta['ends'][v]),'gpr':gpr,'stop':'resident' if v==1 else 'reached','reason':'halted' if v==1 else '', 'tampered':False,'frame_intact':True,'stats':{'halts':1,'splits':2},'memory':copy.deepcopy(memory)})
            args=[meta,{'cpus':cpus,'io_disk':trace},copy.deepcopy(cpus),trace,[copy.deepcopy(memory) for _ in range(4)],(p/'disk.raw').read_bytes(),(p/'expected-disk.raw').read_bytes(),regions[BASE]]
            assert not judge(*args),judge(*args)
            for mutator in (lambda a:a[1]['cpus'][0]['gpr'].__setitem__(13,ROUNDS-1), lambda a:a[3].__setitem__('calls',ROUNDS*2-1), lambda a:a[3]['events'].remove('commit'),lambda a:a[1]['cpus'][2]['stats'].__setitem__('splits',0),lambda a:a[1]['cpus'][1].__setitem__('reason','wrong'),lambda a:a[4][0][0].__setitem__('data','00'),lambda a:a.__setitem__(5,b'bad')):
                bad=copy.deepcopy(args);mutator(bad);assert judge(*bad);controls+=1
    print(f'PASS: two repeated guests assembled; {controls} failing oracle controls; no VM')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--self-check',action='store_true');p.add_argument('--prepare-only',action='store_true')
    for n in ('host','runtime','fexc','out'):p.add_argument('--'+n,type=Path)
    p.add_argument('--case',choices=('reset','disable'),default='reset');p.add_argument('--corrupt-cycle',type=int,choices=range(1,ROUNDS+1),default=0,help='negative control: corrupt one fresh completion before its guest check; the unchanged positive oracle must fail')
    p.add_argument('--lock',type=Path,default=Path.home()/'orca/natemu-raw/.vm.lock');a=p.parse_args()
    if a.self_check:self_check();return 0
    if not a.out or (not a.prepare_only and any(getattr(a,n) is None for n in ('host','runtime','fexc'))):p.error('out and run artifacts required')
    a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=False);meta=fixture(a.case,a.out,a.corrupt_cycle)
    manifest={'purpose':'64 real reset/disable generations under N4 active/sleeping peer load; not unloaded performance','limits':{'VMs':0 if a.prepare_only else 1,'seconds':120},'sources':{str(f):windows.digest(f) for f in (Path(__file__),Path(life.__file__),Path(windows.__file__),ROOT/'crates/host/src/virtio/testing.rs',ROOT/'crates/driver/src/drivers/program.rs')},'artifacts':{}}
    for n in ('host','runtime','fexc'):
        if getattr(a,n):v=getattr(a,n).resolve(strict=True);setattr(a,n,v);manifest['artifacts'][n]={'path':str(v),'sha256':windows.digest(v)}
    save(a.out/'manifest.json',manifest)
    if a.prepare_only:return 0
    cmd=['/usr/bin/time','-lp',str(a.host),str(a.runtime),'run-program',str(a.fexc),str(a.out/'program.txt'),str(a.out),'--watchdog-ms','60000','--ram','0x4000000','--code-shape','--io-disk',str(a.out/'disk.raw'),'--io-script',meta['mode']]
    save(a.out/'command.json',cmd);start=time.monotonic();timeout=False
    with a.lock.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with (a.out/'stdout.log').open('wb') as out,(a.out/'stderr.log').open('wb') as err:
            child=subprocess.Popen(cmd,stdout=out,stderr=err,stdin=subprocess.DEVNULL,start_new_session=True)
            try:child.wait(timeout=120)
            except subprocess.TimeoutExpired:timeout=True;windows.stop_group(child)
            except BaseException:windows.stop_group(child);raise
    errors=[];stderr=(a.out/'stderr.log').read_text(errors='replace')
    if timeout or child.returncode:errors.append('process status/timeout')
    if 'teardown: vcpus 4 joined 4 destroyed 4 vm_destroy 0 children none' not in stderr.splitlines():errors.append('clean teardown')
    try:
        report=json.loads((a.out/'stdout.log').read_text());states,trace,memory=life.final_records(stderr)
        errors+=judge(meta,report,states,trace,memory,(a.out/'disk.raw').read_bytes(),(a.out/'expected-disk.raw').read_bytes(),(a.out/'expected-memory.bin').read_bytes())
    except (ValueError,KeyError,TypeError,IndexError) as e:errors.append('invalid report: '+str(e))
    timing={k:float(v) for k,v in re.findall(r'^(real|user|sys)\s+([0-9.]+)$',stderr,re.M)}
    if set(timing) != {'real','user','sys'}:errors.append('complete host timing')
    result={'pass':not errors,'errors':errors,'seconds':time.monotonic()-start,'host_time':timing,'timeout':timeout,'returncode':child.returncode,'rounds':ROUNDS};save(a.out/'result.json',result);print(json.dumps(result));return int(bool(errors))


if __name__=='__main__':raise SystemExit(main())

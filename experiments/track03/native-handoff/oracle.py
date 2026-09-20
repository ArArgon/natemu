#!/usr/bin/env python3
"""Exact finite trace oracle; no VM API, no producer import, no timing verdict."""
import argparse
import copy
import json
from pathlib import Path

BASE = 0x40000000
EPOCHS = 64

def payload(epoch, phase):
    return [f'{0xa57e91c300000000 | epoch << 16 | phase << 8 | i:016x}' for i in range(4)]

def expected_trace(layout):
    """Independent ordered protocol model. API records are checked separately."""
    sh = {'ish': 3, 'osh': 2}[layout['shareability']]
    route = layout['route']
    assert route in ('copy', 'direct')
    assert layout['cache_line_bytes'] in (64,128)
    tcr = 16 | 1 << 8 | 1 << 10 | sh << 12 | 1 << 23 | 2 << 32
    desc = (BASE + 0x10000) | 3 | 1 << 10 | sh << 8 | 3 << 53
    assert layout == dict(kind='layout', schema='natemu.handoff.v2',
                          shareability=layout['shareability'], route=route,
                          epochs=EPOCHS, bytes=0x24000, cache_line_bytes=layout['cache_line_bytes'],
                          word_stride=128, boot_instruction_pc=layout['boot_instruction_pc'],
                          boot_resume_pc=layout['boot_resume_pc'],
                          reply_instruction_pc=layout['reply_instruction_pc'],
                          reply_resume_pc=layout['reply_resume_pc'], tcr=tcr, descriptor=desc)
    assert BASE <= layout['boot_instruction_pc'] < layout['reply_instruction_pc'] < BASE + 4092
    assert layout['boot_instruction_pc'] % 4 == layout['reply_instruction_pc'] % 4 == 0
    assert layout['boot_resume_pc'] == layout['boot_instruction_pc'] + 4
    assert layout['reply_resume_pc'] == layout['reply_instruction_pc'] + 4
    trace = [layout]
    for cpu in range(2):
        trace.append(dict(kind='boot', cpu=cpu, resume_pc=layout['boot_resume_pc'],
                          syndrome=(0x16 << 26) | (1 << 25) | 0x43,
                          regs=[2, 255, tcr, 0x30d01985, desc, desc]))
    sequence = [0, 0]
    request = 0
    generation = 0

    def command(tag, epoch, phase, alias, cpus, write, data):
        nonlocal request
        request += 1
        for cpu in cpus:
            sequence[cpu] += 1
            regs = [f'{v:016x}' for v in (write, epoch, phase, alias)] + data + [
                f'{cpu:016x}', f'{sequence[cpu]:016x}']
            trace.append(dict(kind='ack', tag=tag, cpu=cpu, request=request,
                              resume_pc=layout['reply_resume_pc'], syndrome=(0x16 << 26) | (1 << 25) | 0x44, regs=regs))

    def observe(tag, epoch, phase, data):
        for alias in (0, 1):
            command(tag, epoch, phase, alias, (0, 1), 0, data)

    def event(kind, epoch, phase, **extra):
        trace.append(dict(kind=kind, epoch=epoch, phase=phase, generation=generation, **extra))

    def transfer(kind, epoch, phase, data):
        trace.append(dict(kind=kind, epoch=epoch, phase=phase, words=data))

    def backend(epoch, phase, old):
        nonlocal generation
        event('grant', epoch, phase)
        if phase == 1:
            if route == 'direct':
                transfer('source_read', epoch, phase, old)
            event('source_capture', epoch, phase, words=old)
        data = payload(epoch, phase)
        if route == 'direct':
            transfer('host_write', epoch, phase, data)
        else:
            command('copy_commit', epoch, phase, epoch % 2, (epoch % 2,), 1, data)
        generation += 1
        event('invalidate', epoch, phase)
        if phase == 1:
            event('reject_old_source', epoch, phase)
        event('complete', epoch, phase)
        observe('released', epoch, phase, data)
        return data

    data = payload(0, 0)
    observe('initial', 0, 0, data)
    for epoch in range(1, EPOCHS + 1):
        observe('prewarm', epoch, 0, data)
        data = backend(epoch, 1, data)
        data = payload(epoch, 2)
        command('resident_write', epoch, 2, epoch % 2, (epoch % 2,), 1, data)
        generation += 1
        event('invalidate', epoch, 2)
        observe('resident_drain', epoch, 2, data)
        event('grant', epoch, 2)
        transfer('host_read' if route == 'direct' else 'copy_export', epoch, 2, data)
        event('complete', epoch, 2)
        observe('returned', epoch, 2, data)
        data = backend(epoch, 3, data)
    trace.append(dict(kind='teardown', cpus=2, epochs=EPOCHS))
    return trace

def check(rows, returncode=0, timed_out=False):
    if returncode != 0 or timed_out:
        raise ValueError('incomplete execution')
    apis = [r for r in rows if r.get('kind') == 'api']
    trace = [r for r in rows if r.get('kind') != 'api']
    if not trace or trace[0].get('kind') != 'layout':
        raise ValueError('missing layout')
    expected = expected_trace(trace[0])
    if len(trace) != len(expected):
        raise ValueError(f'event count {len(trace)} != {len(expected)}')
    for index, (actual, wanted) in enumerate(zip(trace, expected)):
        if actual != wanted:
            raise ValueError(f'event {index}: {actual} != {wanted}')
    if any(set(r) != {'kind', 'call', 'rc'} or r['rc'] != 0 for r in apis):
        raise ValueError('failed/malformed API')
    counts = {}
    for row in apis:
        counts[row['call']] = counts.get(row['call'], 0) + 1
    critical = {'hv_vm_create(NULL)': 1,
                'hv_vm_map(ram, BASE, SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC)': 1,
                'hv_vcpu_create(&v, &out, NULL)': 2, 'hv_vcpu_destroy(v)': 2,
                'hv_vm_unmap(BASE,SIZE)': 1, 'hv_vm_destroy()': 1,
                'hv_vcpu_run(v)': sum(r['kind'] in ('boot', 'ack') for r in trace)}
    if any(counts.get(k) != n for k, n in critical.items()):
        raise ValueError('missing/duplicate VM lifecycle or native run')
    return {'result': 'pass', 'epochs': EPOCHS, 'native_exits': critical['hv_vcpu_run(v)'],
            'scope': 'finite named-cell ownership trace, not universal coherence or timing'}

def synthetic():
    tested = 0
    for sh in ('ish', 'osh'):
        for route in ('copy', 'direct'):
            value = 3 if sh == 'ish' else 2
            layout = dict(kind='layout', schema='natemu.handoff.v2', shareability=sh, route=route,
                          epochs=EPOCHS, bytes=0x24000, cache_line_bytes=128, word_stride=128,
                          boot_instruction_pc=BASE+100, boot_resume_pc=BASE+104,
                          reply_instruction_pc=BASE+200, reply_resume_pc=BASE+204,
                          tcr=16 | 1 << 8 | 1 << 10 | value << 12 | 1 << 23 | 2 << 32,
                          descriptor=(BASE+0x10000) | 3 | 1 << 10 | value << 8 | 3 << 53)
            rows = expected_trace(layout)
            api_counts = {'hv_vm_create(NULL)': 1,
                         'hv_vm_map(ram, BASE, SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC)': 1,
                         'hv_vcpu_create(&v, &out, NULL)': 2, 'hv_vcpu_destroy(v)': 2,
                         'hv_vm_unmap(BASE,SIZE)': 1, 'hv_vm_destroy()': 1,
                         'hv_vcpu_run(v)': sum(r['kind'] in ('ack','boot') for r in rows)}
            rows += [dict(kind='api',call=k,rc=0) for k,n in api_counts.items() for _ in range(n)]
            check(rows)
            mutations = []
            for kind in ('ack','grant','invalidate','reject_old_source','complete','teardown'):
                bad = copy.deepcopy(rows)
                del bad[next(i for i,r in enumerate(bad) if r['kind']==kind)]
                mutations.append(bad)
            for kind, site in (('boot','boot_instruction_pc'),('ack','reply_instruction_pc')):
                bad=copy.deepcopy(rows)
                next(r for r in bad if r['kind']==kind)['resume_pc']=layout[site]
                mutations.append(bad)
            bad=copy.deepcopy(rows)
            next(r for r in bad if r['kind']=='boot')['regs'][3]=0x30d01805
            mutations.append(bad)
            for field in ('cpu','request','resume_pc','syndrome'):
                bad=copy.deepcopy(rows); next(r for r in bad if r['kind']=='ack')[field] += 1
                mutations.append(bad)
            for index in (1,2,3,4,5,6,7,8,9):
                bad=copy.deepcopy(rows); next(r for r in bad if r['kind']=='ack')['regs'][index]='ffffffffffffffff'
                mutations.append(bad)
            bad=copy.deepcopy(rows); next(r for r in bad if r['kind']=='api')['rc']=1; mutations.append(bad)
            bad=copy.deepcopy(rows)
            at=next(i for i,r in enumerate(bad) if r['kind']=='complete')
            bad[at],bad[at-1]=bad[at-1],bad[at]; mutations.append(bad)
            bad=copy.deepcopy(rows)
            next(r for r in bad if r['kind']=='source_capture')['words'][0]='0000000000000000'
            mutations.append(bad)
            for bad in mutations:
                try: check(bad)
                except (ValueError,AssertionError): tested += 1
                else: raise AssertionError('mutant accepted')
            for rc, timeout in ((1,False),(0,True)):
                try: check(rows,rc,timeout)
                except ValueError: tested += 1
                else: raise AssertionError('incomplete execution accepted')
    print(json.dumps({'synthetic_only': True, 'valid_models': 4, 'rejected_mutants': tested}))

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('trace',nargs='?'); parser.add_argument('--self-check',action='store_true')
    args=parser.parse_args()
    if args.self_check: synthetic()
    else:
        if not args.trace: parser.error('trace required')
        rows=[json.loads(line) for line in Path(args.trace).read_text().splitlines()]
        print(json.dumps(check(rows)))

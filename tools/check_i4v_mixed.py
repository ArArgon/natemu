#!/usr/bin/env python3
"""Independent finite mixed protected32 / compatibility32 concurrency oracle.

The old x86 program copies EAX to ECX, stores ECX through EBX, then loads
EAX through EDX. Replacement code stores EDX instead. Paging-off owners keep
identity routing across shared PTE mutations; paged owners follow the PTE.
No runtime walker, generated-code emulator or reported result supplies answers.
"""
import argparse
import copy
import json
from pathlib import Path

import check_i4
from check_i1 import resident_pc
from check_i3_paging import guest_seed as base_guest_seed
from check_i4v_stack_concurrent import check_schedule, check_fault, owners
from check_i4v_alias import descriptor
from i4v_check import exact, require, uint

DATA = 0x100000
SOURCE = 0x102000
PRIVATE = check_i4.PRIVATE
CODE = check_i4.CODE
CODE_NEXT = check_i4.CODE_NEXT
SOURCE_OLD = bytes.fromhex('89c1890b8b02')
SOURCE_NEW = bytes.fromhex('89c189138b02')
BODY_OLD = bytes.fromhex('e70304aa1f2003d5c7fc9f88a4fcdf881f2003d5')
BODY_NEW = bytes.fromhex('e70304aa1f2003d5c5fc9f88a4fcdf881f2003d5')
RET = bytes.fromhex('c0035fd6')
GPR_ROOTS = (4, 7, 5, 6, 8, 9, 10, 11)


def mode(profile, cpu):
    require(profile in (1, 2) and 0 <= cpu < 4, 'bounded mode assignment')
    return (cpu + profile - 1) % 2


def packet(vm, cpu, profile, serial):
    generation = 2 if serial == 3 else 1
    source = SOURCE_NEW if generation == 2 else SOURCE_OLD
    m = mode(profile, cpu)
    return [0x49345641, 1, vm, cpu, 1, generation, m + 1, SOURCE, m,
            int.from_bytes(source, 'little'), SOURCE, serial, 0, 0, 0, 0]


def reply(vm, cpu, profile, serial):
    p = packet(vm, cpu, profile, serial)
    p[0] = 0x49345642
    body = (BODY_NEW if serial == 3 else BODY_OLD) + bytes(4)
    p[13:] = [int.from_bytes(body[i:i+8], 'little') for i in (0, 8, 16)]
    return p


def execute(initial, before, code, capture, replacement):
    """Literal MOV effects; native register copies preserve the admitted zeros."""
    frame, memory = copy.deepcopy(initial), list(before)
    frame['x'][16], frame['x'][30] = code, capture
    frame['x'][7] = frame['x'][4]
    fault = copy.deepcopy(frame)
    value = frame['x'][5] if replacement else frame['x'][7]
    memory[:4] = value.to_bytes(4, 'little')
    frame['x'][4] = int.from_bytes(bytes(memory[128:132]), 'little')
    return frame, memory, fault


def canonical(cpu, profile, initial, completed=False, cleared=False):
    guest = base_guest_seed('cold')
    for gpr, reg in enumerate(GPR_ROOTS):
        guest['gpr'][gpr] = initial['x'][reg] & 0xffffffff
    guest['ymm'][0][:16] = initial['q'][16]
    guest['ymm'][1][:16] = initial['q'][17]
    guest['segments'][0] = [8, 0, 0xffffffff, 0xc09b]
    if mode(profile, cpu) == 0:
        guest['control'][0], guest['control'][3], guest['control'][4] = 0x10001, 0x600, 0
    guest['rip'] = SOURCE + (6 if completed else 0)
    if cpu == 0 and not cleared:
        guest['rflags'] &= ~0x200
    return guest


def check_program(cell, initial, before, cpu, code, capture, replacement):
    frame, after, fault = execute(initial, before, code, capture, replacement)
    _, bank, _ = check_i4.seed(cpu)
    for field, expected in (('id', cpu), ('status', 1), ('initial', initial),
                            ('captured', frame), ('memory_base', DATA + cpu * 256),
                            ('memory_before', before), ('memory_after', after),
                            ('bank_before', bank), ('bank_after', bank),
                            ('case', 'fex_mixed32_changed' if replacement else 'fex_mixed32_old'),
                            ('object_pc', code), ('capture_pc', capture)):
        exact(cell[field], expected, f'CPU{cpu} program.{field}')
    if replacement:
        exact(cell['dirty_faults'], 1, 'one forced retained store permission fault')
        check_fault(cell['dirty_fault'], fault, DATA + cpu * 256, code + 8, permission=True)
    return frame, after, fault


def check_events(record, n, profile, second_frames):
    for cpu, event in owners(record['events'], n, 'events').items():
        for field, value in (('pending', 0), ('token_state', 2), ('notifications', 2),
                             ('consumed', 1), ('deliveries', 1), ('shadow', 0), ('priority', 0),
                             ('vector', 64 + cpu), ('boundary', 3 if cpu in (1, 3) else 2)):
            exact(event[field], value, 'event.' + field)
        waits = uint(event['wait_checks'], 64, 'wait checks')
        if cpu == 0:
            require(waits > 0, 'producer owner must check pending event')
        exact(event['blocked'], [2|4|32|64, 8, 2|16|32|64, 0][cpu] | int(waits > 0),
              'exact event eligibility blockers')
        frame = seed(cpu)[0] if cpu in (1, 3) else second_frames[cpu]
        expected = canonical(cpu, profile, frame, completed=cpu not in (1, 3), cleared=cpu not in (1, 3))
        exact(event['packet'], expected, 'per-owner canonical mode and event boundary')


def shared_tables(n, profile, leaf, source_dirty=False, initial=False):
    """Four-level shared fixture; only paged access may set A/D."""
    accessed = not initial and any(mode(profile, cpu) for cpu in range(n))
    result = [0] * 16384
    entries = {0: 0x2007 | (32 if accessed else 0),
               0x1000: 0x3007 | (32 if accessed else 0),
               0x2000: 0x4007 | (32 if accessed else 0),
               0x3800: leaf, 0x3808: (DATA+4096)|7,
               0x3810: SOURCE | 7 | (32 if accessed else 0) | (64 if source_dirty else 0)}
    for offset, word in entries.items():
        result[offset:offset+8] = word.to_bytes(8, 'little')
    return result


def publication_trace(trace, n):
    """Check per-owner publication before presenting one aggregate boundary."""
    result, published = [], set()
    mutating = False
    for word in trace:
        uint(word,64,'trace word')
        event, cpu = word>>8, word&255
        require(cpu<n,'trace owner range')
        if event==8:
            require(not mutating and not published,'duplicate shared mutation')
            mutating=True
        if event==16:
            require(mutating and cpu not in published,'publication outside mutation or duplicate owner')
            published.add(cpu)
            if len(published)==n:
                result.append(16<<8)
            continue
        if event in (9,10,11):
            exact(published,set(range(n)),'every private code slot published before release')
        result.append(word)
    exact(published,set(range(n)),'all contextual replacement publications')
    return result


def scheduling_view(record, n, profile):
    """Validate real mixed A/D before adapting only table summaries for replay.

    The existing replay owns FIFO/fairness/lifetime checks. Its all-paged leaf
    assertions are not the mixed-mode semantics, and no guest state is changed.
    """
    m = record['maintenance']
    paged = any(mode(profile, cpu) for cpu in range(n))
    history = [(DATA + (4096 if index % 2 == 0 else 0)) | 7 | (32 if paged else 0)
               for index in range(1, 9*n+1)]
    exact(m['leaf_history'], history, 'shared PTE history from paged acknowledgements only')
    require(m['intervals'], 'ordinary execution missing')
    last = max(m['intervals'], key=lambda cell: cell['ordinal'])
    final = history[-1] | (64 if mode(profile, last['id']) else 0)
    exact(m['leaf_final'], final, 'only final paged store may dirty final guest PTE')
    result = copy.deepcopy(record)
    result['trace'] = publication_trace(record['trace'], n)
    result['stack_profile'] = 1  # schedule variant without terminal stack probes
    leaves = [word | 32 for word in history]
    result['maintenance']['leaf_history'] = leaves
    result['maintenance']['leaf_final'] = leaves[-1] | 64
    return result, final


def seed(cpu):
    frame, bank, _ = check_i4.seed(cpu)
    frame['x'][4], frame['x'][7] = 0x10203000+cpu, 0x50607000+cpu
    frame['x'][5] = DATA+cpu*256+128
    frame['x'][11] = DATA+128
    memory = [0xa5]*256
    memory[128:132] = (0x11220000+cpu).to_bytes(4,'little')
    return frame, bank, memory


def initial_pages(n):
    pages = [0xa5]*4096+[0x5a]*4096
    for cpu in range(n):
        at = cpu*256+128
        pages[at:at+4] = (0x11220000+cpu).to_bytes(4,'little')
        pages[4096+at:4096+at+4] = (0x33440000+cpu).to_bytes(4,'little')
    return pages


def check(record, expected_vcpus=None, expected_mixed_profile=None):
    n = uint(record['configured_vcpus'], 32, 'participant count')
    profile = uint(record['mixed_profile'], 32, 'mixed profile')
    require(1 <= n <= 4 and profile in (1, 2), 'finite mixed profile')
    if expected_vcpus is not None:
        exact(n, expected_vcpus, 'requested participants')
    if expected_mixed_profile is not None:
        exact(profile, expected_mixed_profile, 'requested mode assignment')
    for field, value in (('tested_vcpus', n), ('delayed_owner_ms', 50),
                         ('ready', (1<<n)-1), ('finished', (1<<n)-1), ('retained', 0)):
        exact(record[field], value, field)
    exact(record['profile'], 'i4-off-first' if profile==1 else 'i4-paged-first', 'profile name')
    for field,value in (('alias_profile',0),('stack_profile',0),('aliases',[]),('stacks',[])):
        exact(record[field],value,'unrelated profile state '+field)
    exact(record['source_rip'],SOURCE,'fixed source offset')
    exact(record['mixed_compiled'],(1<<n)-1,'all contextual compiles published')
    old = owners(record['cells'], n, 'old programs')
    new = owners(record['new_cells'], n, 'replacement programs')
    faults = owners(record['faults'], n, 'retained native stores')
    mixed = owners(record['mixed'], n, 'mixed-mode records')
    memory = initial_pages(n)
    exact(record['mixed_pages_before'], memory, 'full physical page seeds')
    frames, second_frames, captures = {}, {}, {}
    for cpu in range(n):
        initial, _, _ = seed(cpu)
        capture = uint(old[cpu]['capture_pc'], 64, 'capture PC')
        require(resident_pc(capture), 'capture outside resident code')
        captures[cpu] = capture
        at = cpu * 256
        frame, after, fault = check_program(old[cpu], initial, memory[at:at+256], cpu, CODE, capture, False)
        exact(faults[cpu]['faults'], 1, 'one initial retained store miss')
        exact(faults[cpu]['old_gpa'], DATA+at, 'old physical route')
        check_fault(faults[cpu], fault, DATA+at, CODE+8, permission=False)
        memory[at:at+256] = after
        initial = copy.deepcopy(frame)
        initial['x'][16] = CODE_NEXT
        at += 4096 * mode(profile, cpu)
        frame, after, _ = check_program(new[cpu], initial, memory[at:at+256], cpu, CODE_NEXT, capture, True)
        memory[at:at+256] = after
        frames[cpu] = copy.deepcopy(frame)
        second_frames[cpu] = copy.deepcopy(frame)
    view, final_leaf = scheduling_view(record, n, profile)

    def ordinary(cell, cpu, index, page):
        actual_page = page if mode(profile, cpu) else 0
        at = actual_page * 4096 + cpu * 256
        exact(cell['gpa'], DATA+at, 'mode-specific ordinary physical route')
        before = memory[at:at+256]
        before[:8] = (0x4a00000000000000 | cell['heads'] << 16 | cpu).to_bytes(8, 'little')
        before[128:144] = [0xa5] * 16
        initial = copy.deepcopy(frames[cpu])
        initial['x'][16] = CODE_NEXT
        frame, after, _ = check_program(cell, initial, before, cpu, CODE_NEXT, captures[cpu], True)
        memory[at:at+256], frames[cpu] = after, frame

    counts = check_schedule(view, n, ordinary)
    check_events(record, n, profile, second_frames)
    exact(record['mixed_pages_after'], memory, 'complete physical history and unowned byte guards')
    mask = (1<<n)-1
    paged = any(mode(profile, cpu) for cpu in range(n))
    compose_leaf = (DATA+4096) | 7 | (96 if paged else 0)
    exact(record['coherence'], {'generation': 2, 'acknowledgements': mask, 'active': 0,
        'new_entries': mask, 'new_completed': mask, 'new_references': 0, 'reclaimed': 1,
        'leaf': compose_leaf}, 'closed mapping generation and retained reference lifetime')
    exact(record['source'], {'old_eligible':0,'published':2,'stale_replies':mask,
        'stale_entries':mask,'reuse_blocked':mask,'bytes':list(SOURCE_NEW),
        'old_code':[0]*20+list(RET),'new_code':list(BODY_NEW+RET)}, 'source replacement and private retirement')
    exact(record['mixed_tables_before'],shared_tables(n,profile,DATA|7,initial=True),'initial shared guest tables')
    exact(record['mixed_tables_after'],shared_tables(n,profile,final_leaf,bool(mode(profile,0))),
          'complete table effects from paged fetches/writes only')
    vm = uint(mixed[0]['requests'][0][2], 64, 'VM nonce')
    require(vm != 0, 'nonzero VM identity')
    for cpu in range(n):
        item = mixed[cpu]
        requests = [packet(vm, cpu, profile, serial) for serial in (1, 2, 3)]
        replies = [reply(vm, cpu, profile, serial) for serial in (1, 2, 3)]
        expected = {'mode': mode(profile, cpu), 'incarnation': 1, 'context': mode(profile,cpu)+1,
                    'a20': 1, 'expected': requests[-1], 'requests': requests, 'replies': replies,
                    'stale_replies': 1, 'wrong_owner': 1, 'wrong_mode': 1, 'blank_before_publish': 1,
                    'fetch_gpa': [SOURCE, SOURCE],
                    'routes': [DATA+cpu*256, DATA+cpu*256+4096*mode(profile,cpu)],
                    'source_before': list(SOURCE_OLD),
                    'source_after': list(SOURCE_NEW),
                    'initial': seed(cpu)[0], 'guest_before': canonical(cpu,profile,seed(cpu)[0]),
                    'guest_after': canonical(cpu,profile,frames[cpu],completed=True,cleared=True),
                    'old_code': [0]*20+list(RET), 'new_code': list(BODY_NEW+RET)}
        expected.update(old_memory_after=old[cpu]['memory_after'],
                        new_memory_before=new[cpu]['memory_before'],new_memory_after=new[cpu]['memory_after'])
        for field, value in expected.items():
            exact(item[field], value, f'CPU{cpu} mixed.{field}')
        exact(item['walks'], (6+int(cpu==0)+9*n+counts[cpu]) if mode(profile,cpu) else 0,
              f'CPU{cpu} page walks; identity route must never consult tables')
        for field,value in (('current_el',4),('tcr',16|1<<8|1<<10|3<<12|1<<23|2<<32),
                            ('retries',0),('faults',0),('vector',0),('error',0),('linear',0),('rip',0),('traces',[])):
            exact(item[field],value,f'CPU{cpu} recovery.'+field)
        # The mixed driver records native faults in Round/Interval, not Recovery.
        sctlr=uint(item['sctlr'],64,'SCTLR')
        exact(sctlr & ((1<<0)|(1<<1)|(1<<2)|(1<<3)|(1<<4)|(1<<12)),
              (1<<0)|(1<<2)|(1<<12),'native MMU/cache and permitted unaligned operations')
        require(uint(item['actlr'],64,'ACTLR') & 2 != 0,'native ordering setting')
        require(resident_pc(uint(item['service_pc'],64,'service PC')),'service outside resident code')
        final_page=int((9*n)%2==0) if mode(profile,cpu) else 0
        last=max(record['maintenance']['intervals'],key=lambda c:c['ordinal'])['id']
        leaves=[0]*512
        leaves[256]=descriptor(final_page,readonly=cpu!=last)
        exact(item['lower_leaf'],leaves,f'CPU{cpu} final native route and permissions')
    exact(record['requests'],[p for cpu in range(n) for p in mixed[cpu]['requests']],
          'host-observed compile requests match every resident owner')
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', nargs='+', type=Path)
    parser.add_argument('--vcpus', type=int)
    parser.add_argument('--mixed-profile', type=int, choices=(1,2))
    args = parser.parse_args()
    for path in args.paths:
        try:
            check(json.loads(path.read_text()), args.vcpus, args.mixed_profile)
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise SystemExit(f'{path}: FAIL: {error}') from error
        print(f'{path}: PASS (finite mixed paging-off/compatibility32 concurrency)')


if __name__ == '__main__':
    main()

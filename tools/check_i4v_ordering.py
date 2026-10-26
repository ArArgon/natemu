#!/usr/bin/env python3
"""Independent finite native ordering, aligned XCHG and recovery oracle.

Literal guest effects and the bounded TSO model supply expectations. Producer
packets bind raw FEX output; the published image separately binds the admitted
barriers. A raw LFENCE/SFENCE/MFENCE body control is not whole-fence dependency
qualification, and finite allowed outcomes do not prove universal ordering.
"""

import argparse
import copy
import json
from pathlib import Path

from check_i1 import resident_pc
from i4v_check import exact, require, uint, IPA, TCR
from i4v_ordering_model import check_outcome, check_histogram, check_xchg_chain

PRIVATE = 0x800040000000
CODE = PRIVATE + 0x30000
DATA, RESULTS, RIP = 0x100000, 0x101000, 0x102000
TRIALS = 32
NAMES = ('sb', 'mp', 'lb', 'wrc', 'iriw', 'sb-mfence', 'sb-xchg',
         'xchg', 'xchg-pre', 'xchg-post', 'lfence-control', 'sfence-control', 'mfence-control')
# Retained actual FEX whole-block output, independently disassembled by LLVM:
# i4v-ordering-export-20261026-01. Architectural effects below do not execute it.
SOURCES = tuple(bytes.fromhex(s) for s in (
    'c703010000008b02', 'c70301000000c70201000000', '8b038b0a', '8b03c70201000000',
    'c70301000000', 'c703010000000faef08b02', 'c7030100000087068b02', '87038902',
    '0faee8', '0faef8', '0faef0', '8703'))
BODIES = tuple(bytes.fromhex(s) for s in (
    '340080521f2003d5d4fc9f88a4fcdf881f2003d5',
    '340080521f2003d5d4fc9f881f2003d5b4fc9f88',
    'c4fcdf881f2003d5a7fcdf881f2003d5',
    'c4fcdf881f2003d5340080521f2003d5b4fc9f88',
    '340080521f2003d5d4fc9f88',
    '340080521f2003d5d4fc9f88bf3f03d5a4fcdf881f2003d5',
    '340080521f2003d5d4fc9f884481e4b8a4fcdf881f2003d5',
    'c480e4b81f2003d5a4fc9f88', 'bf3d03d5', 'bf3e03d5', 'bf3f03d5', 'c480e4b8'))
RET = bytes.fromhex('c0035fd6')
DMB, DSB, ISB = (bytes.fromhex(s) for s in ('bf3f03d5', '9f3f03d5', 'df3f03d5'))


def configuration(profile, n, rotation, seed):
    for name, value in (('profile', profile), ('n', n), ('rotation', rotation), ('seed', seed)):
        uint(value, 32, name)
    require(profile < 13 and 1 <= n <= 4 and rotation < n and 0 < seed <= 0xFFFF, 'finite ordering context')
    require(profile >= 7 or n == (2, 2, 2, 3, 4, 2, 2)[profile], 'litmus participant count')
    return {'profile': profile, 'n': n, 'rotation': rotation, 'seed': seed,
            'identity': (profile + 1) | (n << 8) | (rotation << 16) | (seed << 32)}


def role(config, cpu):
    uint(cpu, 32, 'owner')
    require(cpu < config['n'], 'configured owner')
    return (cpu - config['rotation']) % config['n']


def variant(config, cpu):
    p, r = config['profile'], role(config, cpu)
    if p == 0:
        return 0
    if p == 1:
        return 1 if r == 0 else 2
    if p == 2:
        return 3
    if p == 3:
        return (4, 3, 2)[r]
    if p == 4:
        return 4 if r < 2 else 2
    return {5: 5, 6: 6, 7: 7, 8: 7, 9: 7, 10: 8, 11: 9, 12: 10}[p]


def pointers(config, cpu):
    p, r = config['profile'], role(config, cpu)
    reverse = r == 1 if p in (0, 1, 2, 5, 6) else r == 2 if p == 3 else r in (1, 3) if p == 4 else False
    first, second = (DATA + 128, DATA) if reverse else (DATA, DATA + 128)
    return first, RESULTS + cpu * 128 if p >= 7 else second, DATA + 256 + r * 128


def token(config, cpu, trial):
    uint(trial, 32, 'trial index')
    require(trial < TRIALS, 'bounded trial index')
    return (config['seed'] << 16) | ((trial + 1) << 4) | (role(config, cpu) + 1)


def packet(vm, config, cpu):
    uint(vm, 64, 'VM nonce')
    require(vm != 0, 'nonzero compiler VM identity')
    v = variant(config, cpu)
    source = SOURCES[v].ljust(16, b'\0')
    return [0x49345681, 1, vm, cpu, 1, 1, config['identity'], RIP + cpu * 256, 64,
            int.from_bytes(source[:8], 'little'), int.from_bytes(source[8:], 'little'), len(SOURCES[v]),
            len(BODIES[v]), 1, 0, v]


def reply(vm, config, cpu):
    result = packet(vm, config, cpu)
    result[0] = 0x49345682
    body = BODIES[variant(config, cpu)].ljust(32, b'\0')
    result[9:13] = [int.from_bytes(body[i:i + 8], 'little') for i in range(0, 32, 8)]
    return result


def code_image(v):
    require(type(v) is int and 0 <= v < len(BODIES), 'known whole native body')
    body = BODIES[v]
    if v == 6:
        body = body[:12] + DMB + body[12:16] + DMB + body[16:]
    elif v in (7, 11):
        body = DMB + body[:4] + DMB + body[4:]
    elif v == 5:
        body = body[:16] + DSB + ISB + body[16:]
    code = body + RET
    return list(code.ljust(64, b'\0')), len(code)


def native_seed(cpu):
    """Unmodified host seed; pointer/token fixture bindings are applied separately."""
    uint(cpu, 32, 'native seed owner')
    require(cpu < 4, 'native seed owner range')
    frame = {'x': [0x1234000000000000 + r * 0x01010101 for r in range(31)],
             'q': [[(r * 17 + i * 3 + 1) & 255 for i in range(16)] for r in range(32)],
             'sp': PRIVATE + 0x54000, 'nzcv': 0xA0000000, 'fpcr': 0x01000000, 'fpsr': 0x08000001}
    frame['x'][5] = (0, 1, 1, 2)[cpu]
    frame['x'][6] = DATA
    frame['x'][7] = (0, (1 << 64) - 1, (1 << 63) - 1, 1)[cpu]
    frame['x'][11] = DATA + 128
    frame['x'][28] = PRIVATE + 0x40000
    return frame


def trial_frame(initial, value, capture):
    require(resident_pc(capture), 'capture callback in resident executable text')
    uint(value, 32, 'exchange token')
    frame = copy.deepcopy(initial)
    frame['x'][4] = value
    frame['x'][16] = CODE
    frame['x'][30] = capture
    return frame


def expected_native(initial, v, observed=()):
    """Literal x86 register effects plus disassembled finite native scratch effects."""
    frame = copy.deepcopy(initial)
    if v in (0, 1, 3, 4, 5, 6):
        frame['x'][20] = 1
    if v in (0, 3, 5, 6, 7, 11):
        require(len(observed) == 1, 'one observed load/exchange result')
        frame['x'][4] = uint(observed[0], 32, 'EAX result')
    elif v == 2:
        require(len(observed) == 2, 'two independent observed loads')
        frame['x'][4], frame['x'][7] = (uint(x, 32, 'load result') for x in observed)
    else:
        require(not observed, 'writer/fence has no loaded result')
    return frame


def litmus_outcome(config, frames):
    """Convert physical CPU labels to the reference program's declared role order."""
    require(config['profile'] < 7 and set(frames) == set(range(config['n'])), 'complete litmus owner frames')
    ordered = {role(config, cpu): frame for cpu, frame in frames.items()}
    rax = lambda r: ordered[r]['x'][4]
    rcx = lambda r: ordered[r]['x'][7]
    p = config['profile']
    if p == 1:
        outcome = (rax(1), rcx(1))
    elif p == 3:
        outcome = (rax(1), rax(2), rcx(2))
    elif p == 4:
        outcome = (rax(2), rcx(2), rax(3), rcx(3))
    else:
        outcome = (rax(0), rax(1))
    return check_outcome(NAMES[p], outcome)


def check_fault(fault, initial, final, which):
    require(which in (1, 2), 'declared before/after exchange fault')
    expected = initial if which == 1 else final
    exact(fault['frame'], expected, 'full native atomic commit-phase state')
    address = initial['x'][6 if which == 1 else 5]
    pc = CODE + (4 if which == 1 else 16)
    exact((fault['far'], fault['pc'], fault['resume']), (address, pc, pc), 'retry only the faulting native memory instruction')
    esr, spsr = uint(fault['esr'], 64, 'ESR'), uint(fault['spsr'], 64, 'SPSR')
    exact(esr >> 26, 0x25, 'same-EL data abort')
    exact((esr >> 6) & 1, 1, 'write-intent exchange/store fault')
    require(esr & 63 in (range(12, 16) if which == 1 else range(4, 8)),
            'read-only exchange page versus absent later result-store page')
    exact(esr & ((1 << 7) | (1 << 8) | (1 << 10)), 0, 'direct meaningful native FAR')
    exact(spsr & 0x3DF, 0x3C4, 'masked EL1t recovery')
    exact(spsr & 0xF0000000, initial['nzcv'], 'arithmetic flags preserved at fault')


def reset_image(n):
    require(type(n) is int and 1 <= n <= 4, 'configured reset ownership')
    image = [0xA5] * 8192
    for at in [0, 128] + [256 + r * 128 for r in range(n)] + [4096 + cpu * 128 for cpu in range(n)]:
        image[at:at + 4] = [0] * 4
    return image


def expected_image(config, trial, returns=None, final=None):
    image = reset_image(config['n'])
    p = config['profile']
    if p < 7:
        image[:4] = image[128:132] = [1, 0, 0, 0]
        if p == 6:
            for cpu in range(config['n']):
                at = 256 + role(config, cpu) * 128
                image[at:at + 4] = token(config, cpu, trial).to_bytes(4, 'little')
    elif p < 10:
        require(returns is not None and final is not None, 'atomic image requires exact serialization result')
        tokens = {cpu: token(config, cpu, trial) for cpu in range(config['n'])}
        check_xchg_chain(tokens, returns, final)
        image[:4] = final.to_bytes(4, 'little')
        for cpu, value in returns.items():
            at = 4096 + cpu * 128
            image[at:at + 4] = value.to_bytes(4, 'little')
    return image


def initial_tables():
    image = [0] * 16384
    for at, value in ((0, 0x2027), (4096, 0x3027), (8192, 0x4027),
                      (12288 + 256 * 8, DATA | 0x67), (12288 + 257 * 8, RESULTS | 0x67),
                      (12288 + 258 * 8, RIP | 0x27)):
        image[at:at + 8] = value.to_bytes(8, 'little')
    return image


def source_image(config):
    image = [0xCC] * 4096
    for cpu in range(config['n']):
        source = SOURCES[variant(config, cpu)]
        image[cpu * 256:cpu * 256 + len(source)] = source
    return image


def zero_frame():
    return {'x': [0] * 31, 'q': [[0] * 16 for _ in range(32)], 'sp': 0, 'nzcv': 0, 'fpcr': 0, 'fpsr': 0}


def zero_fault():
    return dict(frame=zero_frame(), esr=0, far=0, pc=0, spsr=0, resume=0)


def native_leaf():
    entries = [0] * 512
    attributes = 3 | (1 << 10) | (3 << 8) | (3 << 53)
    entries[256], entries[257] = ((IPA + offset) | attributes for offset in (0x94000, 0x95000))
    return entries


def owner_initial(config, cpu):
    frame = native_seed(cpu)
    frame['x'][4] = token(config, cpu, 0)
    frame['x'][6], frame['x'][5], frame['x'][10] = pointers(config, cpu)
    return frame


def numeric_transport(value):
    if type(value) is dict:
        for k, v in value.items():
            require(type(k) is str, 'JSON field name')
            numeric_transport(v)
    elif type(value) is list:
        for v in value:
            numeric_transport(v)
    elif type(value) is not str:
        uint(value, 64, 'ABI numeric value')


def audit_development(record, producer_directory=None, *, atomic_mode=False):
    """Complete finite report validation; does not assert universal conformance."""
    require(type(record) is dict, 'ordering report object')
    numeric_transport(record)
    exact(set(record), {'case', 'n', 'configured_vcpus', 'tested_vcpus', 'profile', 'rotation', 'seed',
                        'trial_count', 'vm_nonce', 'owners', 'images', 'source_before', 'source_after',
                        'tables_before', 'tables_after', 'common'} | ({'image_bytes','atomic'} if atomic_mode else set()), 'complete ordering report schema')
    exact(record['case'], 'ordering', 'report kind')
    config = configuration(record['profile'], record['n'], record['rotation'], record['seed'])
    n, p = config['n'], config['profile']
    if atomic_mode:
        exact(p,9,'atomic composition uses XCHG/postcommit-store body')
        exact(record['image_bytes'],12288,'three physical pages')
    exact(record['configured_vcpus'], n, 'configured participants')
    exact(record['tested_vcpus'], n, 'actual participants')
    exact(record['trial_count'], TRIALS, 'fixed trial count')
    vm = uint(record['vm_nonce'], 64, 'VM identity')
    require(vm != 0, 'nonzero producer VM identity')
    require(type(record['owners']) is list and len(record['owners']) == n, 'one owner record per participant')
    cells = {c['id']: c for c in record['owners']}
    exact(set(cells), set(range(n)), 'no duplicate or absent participant')
    for key in ('source_before', 'source_after'):
        exact(record[key], source_image(config), 'immutable actual guest source.' + key)
    for key in ('tables_before', 'tables_after'):
        tables=initial_tables()
        if atomic_mode and key=='tables_after':
            tables[12288+257*8:12288+258*8]=(0x103067).to_bytes(8,'little')
        exact(record[key], tables, 'exact guest tables.' + key)
    exact(record['common'], {'compiled': (1 << n) - 1, 'epoch': TRIALS,
                             'ready': [TRIALS] * n + [0] * (4 - n),
                             'done': [TRIALS] * n + [0] * (4 - n),
                             'stopped': (1 << n) - 1}, 'all trial gates completed before reset/termination')
    require(type(record['images']) is list and len(record['images']) == TRIALS, 'one full RAM snapshot per trial')
    images = []
    for raw in record['images']:
        require(type(raw) is str and len(raw) == (24576 if atomic_mode else 16384) and all(c in '0123456789abcdef' for c in raw), 'exact8192-byte RAM hex image')
        images.append(bytes.fromhex(raw))
    captures = {}
    initial = {}
    for cpu, cell in cells.items():
        exact(set(cell), {'id', 'variant', 'trial', 'completed', 'expected', 'request', 'reply', 'walks',
                          'table_reads', 'table_writes', 'routes', 'code', 'code_length', 'isar0',
                          'status', 'detail', 'current_el', 'sctlr', 'actlr', 'tcr', 'capture_pc', 'service_pc',
                          'initial', 'current', 'captured', 'bank_before', 'bank_after', 'published_code',
                          'final_native_leaf', 'compiler_requests', 'trials'}, 'complete owner schema')
        v = variant(config, cpu)
        initial[cpu] = owner_initial(config, cpu)
        req, rep = packet(vm, config, cpu), reply(vm, config, cpu)
        image, length = code_image(v)
        bank = [((i * 37 + 19) & 255) ^ cpu for i in range(512)]
        expected = {'id': cpu, 'variant': v, 'trial': TRIALS, 'completed': TRIALS,
                    'expected': req, 'request': req, 'reply': rep, 'walks': 4 if atomic_mode else 3, 'table_reads': 16 if atomic_mode else 12, 'table_writes': 0,
                    'routes': [DATA, 0x103000 if atomic_mode else RESULTS, RIP + cpu * 256, pointers(config, cpu)[2]],
                    'initial': initial[cpu], 'code': image, 'code_length': length, 'published_code': image,
                    'status': 1, 'detail': 0, 'current_el': 4, 'tcr': TCR,
                    'bank_before': bank, 'bank_after': bank, 'final_native_leaf': native_leaf(),
                    'compiler_requests': [req]}
        if atomic_mode:
            expected['final_native_leaf'][257] += 8192
        for key, value in expected.items():
            exact(cell[key], value, f'owner{cpu}.' + key)
        captures[cpu] = uint(cell['capture_pc'], 64, 'capture PC')
        require(resident_pc(captures[cpu]) and resident_pc(cell['service_pc']), 'resident capture/service callbacks')
        require(cell['sctlr'] & 0x1005 == 0x1005 and cell['sctlr'] & 2 == 0, 'admitted native MMU/cache/alignment state')
        require(cell['actlr'] & 2 != 0, 'hardware TSO enabled per execution context')
        exact((cell['isar0'] >> 20) & 15, 2, 'exact captive LSE encoding admitted by this fixture')
        require(type(cell['trials']) is list and len(cell['trials']) == TRIALS, 'all declared trial records')
        if producer_directory is not None:
            from i4v_ordering_worker import verify_compilations
            verify_compilations(Path(producer_directory) / f'cpu-{cpu}', [req], [rep])
    histogram, total_faults, exchange_orders = {}, 0, []
    for trial in range(TRIALS):
        frames = {cpu: cells[cpu]['trials'][trial]['captured'] for cpu in range(n)}
        if p < 7:
            outcome = litmus_outcome(config, frames)
            histogram[outcome] = histogram.get(outcome, 0) + 1
            expected_ram = expected_image(config, trial)
        elif p < 10:
            values = {cpu: frames[cpu]['x'][4] for cpu in range(n)}
            final = int.from_bytes(bytes(images[trial][:4]), 'little')
            tokens = {cpu: token(config, cpu, trial) for cpu in range(n)}
            exchange_orders.append(check_xchg_chain(tokens, values, final))
            expected_ram = expected_image(config, trial, values, final)
        else:
            expected_ram = expected_image(config, trial)
        if atomic_mode:
            spare=[0x5a]*4096
            for cpu in range(n):spare[cpu*128:cpu*128+4]=[0]*4
            if trial:
                for cpu in range(n):
                    at=4096+cpu*128
                    spare[cpu*128:cpu*128+4]=expected_ram[at:at+4]
                    expected_ram[at:at+4]=[0]*4
            expected_ram+=spare
        exact(images[trial], bytes(expected_ram), f'trial{trial} full physical RAM and untouched bytes')
        for cpu in range(n):
            cell, t = cells[cpu], cells[cpu]['trials'][trial]
            exact(set(t), {'epoch', 'token', 'completed', 'faults', 'captured', 'fault', 'actlr', 'fence_completed'},
                  'complete native trial schema')
            v = variant(config, cpu)
            value = token(config, cpu, trial)
            entered = trial_frame(initial[cpu], value, captures[cpu])
            observations = (frames[cpu]['x'][4], frames[cpu]['x'][7]) if v == 2 else ((frames[cpu]['x'][4],) if v in (0, 3, 5, 6, 7) else ())
            expected_frame = expected_native(entered, v, observations)
            exact(t['captured'], expected_frame, 'whole native result, scratch, flags/vector/control preservation')
            which = 2 if atomic_mode and trial==0 else p - 7 if p in (8, 9) and cpu == config['rotation'] else 0
            for key, expected in (('epoch', trial + 1), ('token', value), ('completed', 1), ('faults', int(which != 0)),
                                  ('fence_completed', int(p >= 10))):
                exact(t[key], expected, 'trial.' + key)
            require(t['actlr'] & 2 != 0, 'TSO enabled during every native trial')
            require(type(t['fault']) is list and len(t['fault']) == 2, 'bounded fault slots')
            if which:
                exact(set(t['fault'][0]), set(zero_fault()), 'complete fault transport')
                check_fault(t['fault'][0], entered, expected_frame, which)
                total_faults += 1
            else:
                exact(t['fault'][0], zero_fault(), 'ordinary warmed block executes without native faults')
            exact(t['fault'][1], zero_fault(), 'no second/replayed fault')
            if trial == TRIALS - 1:
                exact(cell['current'], expected_frame, 'resident final captured state')
                exact(cell['captured'], expected_frame, 'Shared final captured state')
    if p < 7:
        check_histogram(NAMES[p], list(histogram.items()), TRIALS)
    return {'profile': NAMES[p], 'owners': n, 'trials': TRIALS, 'native_entries': n * TRIALS,
            'native_faults': total_faults, 'compilations': n,
            'outcomes': [{'values': list(values), 'count': count} for values, count in sorted(histogram.items())],
            'exchange_chains': len(exchange_orders), 'raw_fence_controls': n * TRIALS if p >= 10 else 0}


def bind_requested(record, expected_profile=None, expected_vcpus=None, expected_rotation=None, expected_seed=None):
    require(type(record) is dict, 'expected native ordering report')
    require(all(k in record for k in ('profile', 'n', 'rotation', 'seed')), 'missing declared ordering identity')
    config = configuration(record['profile'], record['n'], record['rotation'], record['seed'])
    for key, value in (('profile', expected_profile), ('n', expected_vcpus),
                       ('rotation', expected_rotation), ('seed', expected_seed)):
        if value is not None:
            uint(value, 32, 'requested ' + key)
            exact(record[key], value, 'requested ordering ' + key)
    return config


def check(record, expected_profile=None, expected_vcpus=None, expected_rotation=None, expected_seed=None,
          producer_directory=None):
    bind_requested(record, expected_profile, expected_vcpus, expected_rotation, expected_seed)
    return audit_development(record, producer_directory)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports', nargs='+', type=Path)
    parser.add_argument('--profile', type=int)
    parser.add_argument('--vcpus', type=int)
    parser.add_argument('--rotation', type=int)
    parser.add_argument('--seed', type=int)
    args = parser.parse_args()
    for path in args.reports:
        check(json.loads(path.read_text()), args.profile, args.vcpus, args.rotation, args.seed)
        print(f'{path}: PASS')

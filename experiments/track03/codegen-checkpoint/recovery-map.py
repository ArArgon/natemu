#!/usr/bin/env python3
"""Descriptor model and partial FEX diagnostic-text consumer; never runs generated code."""
import argparse
import copy
import json
import re
from pathlib import Path

GPRS = ['RAX', 'RBX', 'RCX', 'RDX', 'RSI', 'RDI', 'RBP', 'RSP'] + [f'R{i}' for i in range(8, 16)]
ARITH = ['CF', 'PF', 'AF', 'ZF', 'SF', 'OF']
BANKS = ['x87', 'MXCSR', 'other_rflags', 'control', 'debug', 'segment', 'event_progress']
LIVE = ['pending_events', 'APIC', 'time', 'memory_effects']
CATALOG = GPRS + [f'XMM{i}.low' for i in range(16)] + [f'YMM{i}.high' for i in range(16)] + ARITH + ['RF', 'RIP'] + BANKS + LIVE


def require(condition, message):
    if not condition:
        raise ValueError(message)


def expected_roles(fixture):
    roles = {r: 64 for r in GPRS}
    roles.update({f'XMM{i}.low': 128 for i in range(16)})
    if fixture == 'H':
        roles.update({'YMM0.high': 128, 'nzcv': 32, 'pf_raw': 64, 'af_raw': 64})
    return roles


def provenance(fixture, role):
    if fixture == 'L' and role == 'RCX':
        return 'completed-ADD64-result'
    if fixture == 'L' and role == 'XMM0.low':
        return 'completed-PXOR-result'
    if fixture == 'H' and role in ['XMM0.low', 'YMM0.high']:
        return 'completed-VPADDD-result'
    if role in ['nzcv', 'pf_raw', 'af_raw']:
        return 'current-calculated-flags'
    return 'entry-value-retained-at-site'


def export(evidence):
    """Validate supplied witnesses, then serialize dispositions; does not prove witnesses."""
    e = copy.deepcopy(evidence)
    require(e['schema'] == 'natemu.synthetic-recovery-input.v1', 'input schema')
    require(e['fixture'] in ['L', 'H'], 'fixture')
    fixture = e['fixture']
    require(e['catalog'] == CATALOG, 'diagnostic catalog mismatch')
    site = e['site']
    require(site['phase'] == 'MOV.data.precommit' and site['RF'] == 0, 'phase/RF')
    require(site['saved_page_fault_RF'] == 1, 'saved fault RF')
    require(site['access'] == 'MOV64.RAX.from.RBX' and site['committed'] is False, 'access/commit')
    require(site['code_pin'] == e['authority']['code_pin'] and site['code_pin'], 'code lifetime')
    require(site['frame_pin'] == e['authority']['frame_pin'] and site['frame_pin'], 'frame lifetime')
    require(type(site['native_offset']) is int and 0 <= site['native_offset'] < site['code_size'], 'native site')
    require(type(site['guest_mov']) is int and 0 <= site['guest_mov'] < 2**64, 'guest site')
    if fixture == 'L':
        require(site['guest_mov'] == e['entry']['guest_start'] + 7, 'L decoded origin')
    else:
        require(e['entry']['decoded_H_mov'] == site['guest_mov'], 'H decoded origin')
    require(e['entry']['id'] == e['authority']['entry_id'], 'entry identity')
    roles = expected_roles(fixture)
    require(set(e['roots']) == set(roles), 'missing/extra working root')
    for role, width in roles.items():
        root = e['roots'][role]
        require(root['width'] == width and root['value_id'], 'root width/identity: ' + role)
        require(root['provenance'] == provenance(fixture, role), 'root provenance: ' + role)
        require(root['site_id'] == site['id'] and root['live'] is True, 'root lifetime: ' + role)
        loc = root['location']
        require(loc['frame_pin'] == site['frame_pin'], 'location frame: ' + role)
        require(loc['kind'] in ['register', 'spill'], 'unresolved root: ' + role)
        if loc['kind'] == 'register':
            require(loc['bank'] == ('GPR' if width <= 64 else 'SIMD128'), 'register bank: ' + role)
            require(type(loc['index']) is int and 0 <= loc['index'] < (31 if width <= 64 else 32), 'register index')
            require(loc['occupant'] == root['value_id'], 'register ownership: ' + role)
        else:
            require(type(loc['offset']) is int and loc['offset'] >= 0 and loc['offset'] % (width // 8) == 0, 'spill offset')
            require(loc['offset'] + width // 8 <= e['authority']['spill_bytes'], 'spill extent')
            require(loc['initialized_at_site'] is True and loc['occupant'] == root['value_id'], 'uninitialized/stale spill')
    # Different values cannot occupy overlapping storage at this same fault site.
    occupied = []
    for role, root in e['roots'].items():
        loc = root['location']
        key = (loc['kind'], loc.get('bank'))
        start = loc['index'] if loc['kind'] == 'register' else loc['offset']
        end = start + (1 if loc['kind'] == 'register' else root['width'] // 8)
        for oldkey, lo, hi, value in occupied:
            require(key != oldkey or end <= lo or start >= hi or value == root['value_id'], 'overlapping distinct root storage')
        occupied.append((key, start, end, root['value_id']))
    fields = {r: {'kind': 'root', 'role': r} for r in roles if r not in ['nzcv', 'pf_raw', 'af_raw']}
    needed_backing = [f'YMM{i}.high' for i in range(0 if fixture == 'L' else 1, 16)]
    require(set(e['backing']) == set(needed_backing), 'high-half backing disposition')
    require(set(e['authority']['backing_versions']) == set(needed_backing), 'backing authority catalog')
    require(set(e['authority']['bank_versions']) == set(BANKS), 'bank authority catalog')
    require(all(type(v) is int and v >= 0 for table in ['backing_versions', 'bank_versions']
                for v in e['authority'][table].values()), 'invalid authority version')
    for field in needed_backing:
        b = e['backing'][field]
        require(b['field'] == field and b['width'] == 128, 'backing field/width')
        require(b['entry_id'] == e['entry']['id'] and b['initialized'] is True, 'stale/uninitialized entry backing')
        require(b['version'] == e['authority']['backing_versions'].get(field), 'stale backing version')
        require(b['dirty'] is False and b['unchanged_since_entry'] is True, 'dirty high backing')
        require(b['owner_pin'] == e['authority']['bank_pin'] and b['owner_pin'], 'backing owner lifetime')
        fields[field] = {'kind': 'backing', 'binding': b}
    require(set(e['banks']) == set(BANKS) and set(e['live_owners']) == set(LIVE), 'owner catalog')
    for field in BANKS:
        b = e['banks'][field]
        require(b['owner_pin'] == e['authority']['bank_pin'] and b['owner_pin'], 'semantic bank lifetime')
        require(b['version'] == e['authority']['bank_versions'].get(field), 'stale semantic bank')
        require(b['phase_id'] == site['phase_id'], 'semantic phase mismatch')
        fields[field] = {'kind': 'retained-owner', 'binding': b}
    for field in LIVE:
        require(e['live_owners'][field] == 'retain-current-not-entry-rollback', 'live authority rollback')
        fields[field] = {'kind': 'live-owner', 'action': e['live_owners'][field]}
    if fixture == 'L':
        recipe = {'name': 'inverse-add64', 'inputs': ['RCX', 'RDX']}
    else:
        require(type(e['carry_inverted']) is bool, 'missing carry inversion tag')
        recipe = {'name': 'fex-nzcv-pf-af', 'inputs': ['nzcv', 'pf_raw', 'af_raw'], 'carry_inverted': e['carry_inverted']}
    fields.update({f: {'kind': 'recipe', 'recipe': recipe, 'output': f} for f in ARITH})
    fields['RF'] = {'kind': 'constant', 'value': 0}
    fields['RIP'] = {'kind': 'constant', 'value': site['guest_mov']}
    require(set(fields) == set(CATALOG), 'incomplete dispositions')
    return {'schema': 'natemu.synthetic-recovery-map.v1', 'status': 'synthetic supplied witnesses; not observed allocation',
            'fixture': fixture, 'site': site, 'fields': fields, 'evidence': e, 'saved_page_fault_RF': 1}


def check_map(descriptor):
    require(descriptor == export(descriptor['evidence']), 'map differs from validated field dispositions')



def import_trace(text):
    """Parse retained five-root/v2 working-root text; reject full-map publication."""
    patterns = {
        'BEGIN': r'entry=([0-9a-f]+)(?: format=(working32-v2))?',
        'AUTHORITY': r'entry=([0-9a-f]+) status=(missing)',
        'SITE': r'entry=([0-9a-f]+) access=(\d+) offset=(\d+) dst=x(\d+) address=x(\d+)',
        'ROOT': r'entry=([0-9a-f]+) access=(\d+) guest_offset=(\d+) role=([A-Za-z0-9_.]+) location=(x\d+|v\d+|sp\+\d+)',
        'END': r'entry=([0-9a-f]+) size=(\d+)',
    }
    names = {'sum': 'RCX', 'addend': 'RDX', 'old_RAX': 'RAX', 'address_RBX': 'RBX', 'XMM0_low128': 'XMM0.low'}
    active, completed, abandoned = None, [], 0
    for line in text.splitlines():
        if not line.startswith('NATEMU_'):
            continue
        kind, _, payload = line.partition(' ')
        kind = kind.removeprefix('NATEMU_')
        require(kind in patterns, 'producer rejection/unknown marker')
        match = re.fullmatch(patterns[kind], payload)
        require(match is not None, 'malformed producer marker')
        parts = match.groups()
        entry = int(parts[0], 16)
        if kind == 'BEGIN':
            abandoned += active is not None
            active = {'entry': entry, 'roots': {}, 'site': None, 'format': parts[1] or 'five-root-v1', 'authority': None}
            continue
        require(active is not None and active['entry'] == entry, 'unbracketed/cross-entry marker')
        role_names = {r: r for r in expected_roles('L')} if active['format'] == 'working32-v2' else names
        if kind == 'AUTHORITY':
            require(active['format'] == 'working32-v2' and active['authority'] is None and active['site'] is None and not active['roots'], 'unexpected authority boundary')
            active['authority'] = 'missing'
        elif kind == 'SITE':
            require(active['site'] is None, 'duplicate site')
            access, offset, dst, address = map(int, parts[1:])
            require(dst < 31 and address < 31, 'native transfer registers')
            active['site'] = {'access': access, 'offset': offset, 'destination': dst, 'address': address}
        elif kind == 'ROOT':
            access, guest_offset, role, location = parts[1:]
            require(role in role_names and role not in active['roots'] and int(guest_offset) == 7, 'unknown/duplicate role/wrong fixture origin')
            vector = role_names[role].startswith('XMM')
            if location.startswith('sp+'):
                require(int(location[3:]) % (16 if vector else 8) == 0, 'unaligned producer spill')
            else:
                require(location[0] == ('v' if vector else 'x') and int(location[1:]) < (32 if vector else 31), 'producer register class')
            active['roots'][role] = {'access': int(access), 'role': role_names[role], 'location': location}
        else:
            size = int(parts[1])
            require(size > 0, 'empty code object')
            require(active['format'] != 'working32-v2' or active['authority'] == 'missing', 'missing explicit authority boundary')
            if active['roots'] or active['site'] is not None:
                require(set(active['roots']) == set(role_names) and active['site'] is not None, 'incomplete working-root attempt')
                site = active['site']
                require(0 <= site['offset'] < size, 'site outside emitted object')
                require(all(r['access'] == site['access'] for r in active['roots'].values()), 'access identity mismatch')
                require(all(r['location'] != 'x' + str(site['destination']) for r in active['roots'].values()), 'live root aliases load destination')
                active['code_size'] = size
                completed.append(active)
            active = None
    require(active is None, 'unterminated emission attempt')
    require(completed, 'no complete working-root attempt')
    return {'status': 'partial producer trace; full-map publication rejected', 'publishable': False,
            'attempts': completed, 'discarded_abandoned_attempts': abandoned,
            'missing': ([] if all(a['format'] == 'working32-v2' for a in completed) else ['27 remaining working-register roles for L']) + ['upper-half entry currency/owner bindings',
                        'semantic/live owner and phase/pin witnesses', 'H fixture and dirty-high/flag roots'],
            'limit': 'parsed producer ABI only; no observed trace is bundled and no allocation validity is inferred'}

def example(fixture):
    """Invented resident spill locations solely for checker tests, not FEX output."""
    e = {'schema': 'natemu.synthetic-recovery-input.v1', 'fixture': fixture, 'catalog': CATALOG,
         'entry': {'id': 'entry-1', 'guest_start': 4096, 'decoded_H_mov': 4100},
         'site': {'id': 'site-1', 'phase': 'MOV.data.precommit', 'phase_id': 'phase-1', 'RF': 0,
                  'saved_page_fault_RF': 1, 'access': 'MOV64.RAX.from.RBX', 'committed': False,
                  'code_pin': 'code-1', 'frame_pin': 'frame-1', 'native_offset': 64, 'code_size': 128,
                  'guest_mov': 4103 if fixture == 'L' else 4100},
         'authority': {'entry_id': 'entry-1', 'code_pin': 'code-1', 'frame_pin': 'frame-1',
                       'bank_pin': 'bank-1', 'spill_bytes': 1024, 'backing_versions': {}, 'bank_versions': {}},
         'roots': {}, 'backing': {}, 'banks': {}, 'live_owners': {f: 'retain-current-not-entry-rollback' for f in LIVE},
         'carry_inverted': True}
    for index, (role, width) in enumerate(expected_roles(fixture).items()):
        e['roots'][role] = {'value_id': 'value-' + role, 'width': width, 'site_id': 'site-1', 'live': True, 'provenance': provenance(fixture, role),
                            'location': {'kind': 'spill', 'offset': index * 16, 'frame_pin': 'frame-1',
                                         'initialized_at_site': True, 'occupant': 'value-' + role}}
    for i in range(0 if fixture == 'L' else 1, 16):
        field = f'YMM{i}.high'
        e['authority']['backing_versions'][field] = 1
        e['backing'][field] = {'field': field, 'width': 128, 'entry_id': 'entry-1', 'initialized': True, 'version': 1, 'dirty': False,
                              'unchanged_since_entry': True, 'owner_pin': 'bank-1'}
    for field in BANKS:
        e['authority']['bank_versions'][field] = 2
        e['banks'][field] = {'owner_pin': 'bank-1', 'version': 2, 'phase_id': 'phase-1'}
    return e


def self_check(directory):
    directory.mkdir()  # Never overwrite a retained test directory.
    results = []
    def check(name, e, valid):
        (directory / (name + '.input.json')).write_text(json.dumps(e, indent=2) + '\n')
        try:
            result = export(e)
            check_map(result)
        except (ValueError, KeyError, TypeError) as error:
            require(not valid, name + ': unexpected rejection: ' + str(error))
            results.append({'case': name, 'rejected': str(error)})
        else:
            require(valid, name + ': accepted invalid evidence')
            (directory / (name + '.map.json')).write_text(json.dumps(result, indent=2) + '\n')
            results.append({'case': name, 'accepted': True})
    for fixture in ['L', 'H']:
        check(fixture + '-valid', example(fixture), True)
    registers = example('L')
    for role, bank, index in [('RAX', 'GPR', 0), ('XMM0.low', 'SIMD128', 0)]:
        registers['roots'][role]['location'] = {'kind': 'register', 'bank': bank, 'index': index,
                                              'frame_pin': 'frame-1', 'occupant': 'value-' + role}
    check('L-registers-valid', registers, True)
    mutations = [
        ('RF-entry-one', 'L', lambda e: e['site'].update(RF=1)),
        ('RF-saved-zero', 'H', lambda e: e['site'].update(saved_page_fault_RF=0)),
        ('postcommit', 'L', lambda e: e['site'].update(committed=True)),
        ('stale-entry', 'L', lambda e: e['backing']['YMM0.high'].update(entry_id='old-entry')),
        ('missing-authority-version', 'L', lambda e: (e['authority']['backing_versions'].pop('YMM0.high'), e['backing']['YMM0.high'].update(version=None))),
        ('missing-bank-authority-version', 'L', lambda e: (e['authority']['bank_versions'].pop('MXCSR'), e['banks']['MXCSR'].update(version=None))),
        ('null-authority-version', 'L', lambda e: e['authority']['bank_versions'].update(MXCSR=None)),
        ('stale-version', 'L', lambda e: e['backing']['YMM0.high'].update(version=0)),
        ('dirty-high', 'L', lambda e: e['backing']['YMM0.high'].update(dirty=True)),
        ('H-entry-high-root', 'H', lambda e: e['roots']['YMM0.high'].update(provenance='entry-value-retained-at-site')),
        ('H-missing-high-root', 'H', lambda e: e['roots'].pop('YMM0.high')),
        ('H-stale-high-backing', 'H', lambda e: e['backing'].update({'YMM0.high': example('L')['backing']['YMM0.high']})),
        ('uninitialized-spill', 'L', lambda e: e['roots']['RAX']['location'].update(initialized_at_site=False)),
        ('expired-frame', 'L', lambda e: e['roots']['RAX']['location'].update(frame_pin='expired')),
        ('overlap', 'L', lambda e: e['roots']['RBX']['location'].update(offset=0)),
        ('wrong-site', 'L', lambda e: e['roots']['RAX'].update(site_id='later-site')),
        ('missing-flag-root', 'H', lambda e: e['roots'].pop('pf_raw')),
        ('missing-inversion', 'H', lambda e: e.pop('carry_inverted')),
        ('old-event-phase', 'L', lambda e: e['banks']['event_progress'].update(phase_id='entry-phase')),
        ('live-rollback', 'L', lambda e: e['live_owners'].update(time='restore-entry')),
        ('missing-catalog-field', 'L', lambda e: e['catalog'].remove('MXCSR')),
        ('L-map-for-H', 'L', lambda e: e.update(fixture='H')),
    ]
    for name, fixture, mutate in mutations:
        e = example(fixture)
        # Detach the catalog before mutations; never alter module policy.
        e = copy.deepcopy(e)
        mutate(e)
        check(name, e, False)
    # Serialized output is checked too, not only the supplied witness structure.
    for name, mutate in [
        ('map-RF-one', lambda m: m['fields']['RF'].update(value=1)),
        ('map-old-RAX-rebound', lambda m: m['fields']['RAX'].update(role='RBX')),
        ('map-missing-MXCSR', lambda m: m['fields'].pop('MXCSR')),
    ]:
        descriptor = export(example('L'))
        mutate(descriptor)
        (directory / (name + '.map.json')).write_text(json.dumps(descriptor, indent=2) + '\n')
        try:
            check_map(descriptor)
        except ValueError as error:
            results.append({'case': name, 'rejected': str(error)})
        else:
            raise ValueError(name + ': accepted modified map')
    trace = '\n'.join([
        'NATEMU_BEGIN entry=1000',
        'NATEMU_SITE entry=1000 access=42 offset=64 dst=x5 address=x3',
        *[f'NATEMU_ROOT entry=1000 access=42 guest_offset=7 role={role} location={loc}'
          for role, loc in [('sum', 'x0'), ('addend', 'x1'), ('old_RAX', 'x2'),
                            ('address_RBX', 'x3'), ('XMM0_low128', 'v0')]],
        'NATEMU_END entry=1000 size=128'])
    working = '\n'.join([
        'NATEMU_BEGIN entry=1000 format=working32-v2',
        'NATEMU_AUTHORITY entry=1000 status=missing',
        'NATEMU_SITE entry=1000 access=42 offset=64 dst=x5 address=x3',
        *[f'NATEMU_ROOT entry=1000 access=42 guest_offset=7 role={role} location=sp+{i * 16}'
          for i, role in enumerate(expected_roles('L'))],
        'NATEMU_END entry=1000 size=128'])
    for name, text, valid in [
        ('trace-partial', trace, True),
        ('trace-abandoned-restart', 'NATEMU_BEGIN entry=abcd\n' + trace, True),
        ('trace-unfinished', trace.rsplit('\n', 1)[0], False),
        ('trace-cross-access', trace.replace('access=42 offset', 'access=43 offset'), False),
        ('trace-destination-alias', trace.replace('dst=x5', 'dst=x2'), False),
        ('trace-producer-rejection', trace + '\nNATEMU_REJECT unsupported', False),
        ('working32-partial', working, True),
        ('working32-restart', 'NATEMU_BEGIN entry=abcd\n' + working, True),
        ('working32-missing-authority', working.replace('NATEMU_AUTHORITY entry=1000 status=missing\n', ''), False),
        ('working32-invented-authority', working.replace('status=missing', 'status=valid'), False),
        ('working32-missing-role', working.replace('role=R15', 'role=R14'), False),
        ('working32-wrong-class', working.replace('role=XMM15.low location=sp+496', 'role=XMM15.low location=x1'), False),
        ('working32-cross-version', working.replace(' format=working32-v2', ''), False),
    ]:
        (directory / (name + '.synthetic.txt')).write_text(text + '\n')
        try:
            partial = import_trace(text)
        except ValueError as error:
            require(not valid, name + ': unexpected trace rejection')
            results.append({'case': name, 'rejected': str(error)})
        else:
            require(valid and partial['publishable'] is False, name + ': invalid publishable trace')
            (directory / (name + '.partial.json')).write_text(json.dumps(partial, indent=2) + '\n')
            results.append({'case': name, 'parsed': True, 'publishable': False})
    (directory / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    print(f'PASS: {len(results)} synthetic cases; no observed allocation or guest execution')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--self-check', type=Path, metavar='FRESH_DIRECTORY')
    group.add_argument('--input', type=Path)
    group.add_argument('--trace', type=Path, help='Import versioned producer text; exit 2 because full-state inputs are missing')
    group.add_argument('--map', type=Path, help='Validate a serialized model descriptor')
    args = parser.parse_args()
    if args.self_check:
        self_check(args.self_check)
    elif args.trace:
        print(json.dumps(import_trace(args.trace.read_text()), indent=2))
        raise SystemExit(2)
    elif args.map:
        check_map(json.loads(args.map.read_text()))
        print('PASS: supplied-witness descriptor consistency only')
    else:
        print(json.dumps(export(json.loads(args.input.read_text())), indent=2))

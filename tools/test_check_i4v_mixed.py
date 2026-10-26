import copy
import unittest

import check_i4v_mixed as oracle
import check_i4_pressure


def fault(frame, cpu, code, permission):
    return {'frame': copy.deepcopy(frame), 'far': oracle.DATA+cpu*256, 'pc': code+8,
            'esr': (0x25<<26)|64|(15 if permission else 7), 'spsr': frame['nzcv']|0x3c4}


def program(initial, memory, cpu, replacement, capture):
    code = oracle.CODE_NEXT if replacement else oracle.CODE
    frame, after, prefix = oracle.execute(initial, memory, code, capture, replacement)
    bank = oracle.seed(cpu)[1]
    result = {'id': cpu, 'case':'fex_mixed32_changed' if replacement else 'fex_mixed32_old', 'status': 1, 'initial': copy.deepcopy(initial), 'captured': frame,
              'memory_base': oracle.DATA+cpu*256, 'memory_before': list(memory),
              'memory_after': after, 'bank_before': bank, 'bank_after': list(bank),
              'object_pc': code, 'capture_pc': capture}
    if replacement:
        result.update(dirty_faults=1, dirty_fault=fault(prefix, cpu, code, True))
    return result, prefix


def fixture(n=4, profile=1):
    record = check_i4_pressure.known_result(n)
    record['mixed_profile'] = profile
    record.update(alias_profile=0,stack_profile=0,aliases=[],stacks=[])
    record['profile'] = 'i4-off-first' if profile == 1 else 'i4-paged-first'
    record['source_rip']=oracle.SOURCE
    record['mixed_compiled']=(1<<n)-1
    ix=record['trace'].index(16<<8)
    record['trace'][ix:ix+1]=[(16<<8)|cpu for cpu in range(n)]
    capture = oracle.PRIVATE+0x400
    memory = oracle.initial_pages(n)
    record['mixed_pages_before'] = list(memory)
    frames, second = {}, {}
    for cpu in range(n):
        at = cpu*256
        cell, prefix = program(oracle.seed(cpu)[0], memory[at:at+256], cpu, False, capture)
        record['cells'][cpu] = cell
        memory[at:at+256] = cell['memory_after']
        record['faults'][cpu] = dict(fault(prefix, cpu, oracle.CODE, False),
                                     id=cpu, faults=1, old_gpa=oracle.DATA+at)
        initial = copy.deepcopy(cell['captured'])
        initial['x'][16] = oracle.CODE_NEXT
        at += 4096*oracle.mode(profile,cpu)
        cell, _ = program(initial, memory[at:at+256], cpu, True, capture)
        record['new_cells'][cpu] = cell
        memory[at:at+256] = cell['memory_after']
        frames[cpu] = copy.deepcopy(cell['captured'])
        second[cpu] = copy.deepcopy(cell['captured'])
    intervals = []
    for previous in record['maintenance']['intervals']:
        cpu = previous['id']
        page = int(previous['heads']%2 == 0) if oracle.mode(profile,cpu) else 0
        at = page*4096+cpu*256
        before = memory[at:at+256]
        before[:8] = (0x4a00000000000000 | previous['heads']<<16 | cpu).to_bytes(8,'little')
        before[128:144] = [0xa5]*16
        initial = copy.deepcopy(frames[cpu])
        initial['x'][16] = oracle.CODE_NEXT
        cell, _ = program(initial, before, cpu, True, capture)
        cell.update({key:previous[key] for key in ('ordinal','heads','epoch')})
        cell['gpa'] = oracle.DATA+at
        intervals.append(cell)
        frames[cpu], memory[at:at+256] = copy.deepcopy(cell['captured']), cell['memory_after']
    m = record['maintenance']
    m['intervals'] = intervals
    paged = any(oracle.mode(profile,cpu) for cpu in range(n))
    m['leaf_history'] = [(oracle.DATA+(4096 if head%2==0 else 0))|7|(32 if paged else 0)
                         for head in range(1,9*n+1)]
    m['leaf_final'] = m['leaf_history'][-1] | (64 if oracle.mode(profile,intervals[-1]['id']) else 0)
    record['coherence']['leaf'] = (oracle.DATA+4096)|7|(96 if paged else 0)
    for event in record['events']:
        cpu = event['id']
        initial = cpu in (1,3)
        event['packet'] = oracle.canonical(cpu,profile,oracle.seed(cpu)[0] if initial else second[cpu],
                                          completed=not initial,cleared=not initial)
    record['mixed'] = []
    for cpu in range(n):
        requests = [oracle.packet(123,cpu,profile,serial) for serial in (1,2,3)]
        record['mixed'].append({'id':cpu,'mode':oracle.mode(profile,cpu),'incarnation':1,
            'context':oracle.mode(profile,cpu)+1,'a20':1,'expected':requests[-1],
            'requests':requests,'replies':[oracle.reply(123,cpu,profile,serial) for serial in (1,2,3)],
            'stale_replies':1,'wrong_owner':1,'wrong_mode':1,'blank_before_publish':1,
            'fetch_gpa':[oracle.SOURCE]*2,
            'routes':[oracle.DATA+cpu*256,oracle.DATA+cpu*256+4096*oracle.mode(profile,cpu)],
            'source_before':list(oracle.SOURCE_OLD),
            'source_after':list(oracle.SOURCE_NEW),
            'initial':oracle.seed(cpu)[0], 'guest_before':oracle.canonical(cpu,profile,oracle.seed(cpu)[0]),
            'guest_after':oracle.canonical(cpu,profile,frames[cpu],completed=True,cleared=True),
            'old_code':[0]*20+list(oracle.RET),'new_code':list(oracle.BODY_NEW+oracle.RET)})
    record['mixed_pages_after'] = memory
    record['mixed_tables_before']=oracle.shared_tables(n,profile,oracle.DATA|7,initial=True)
    record['mixed_tables_after']=oracle.shared_tables(n,profile,m['leaf_final'],bool(oracle.mode(profile,0)))
    record['source']={'old_eligible':0,'published':2,'stale_replies':(1<<n)-1,
        'stale_entries':(1<<n)-1,'reuse_blocked':(1<<n)-1,'bytes':list(oracle.SOURCE_NEW),
        'old_code':[0]*20+list(oracle.RET),'new_code':list(oracle.BODY_NEW+oracle.RET)}
    record['requests']=[p for owner in record['mixed'] for p in owner['requests']]
    for cpu,item in enumerate(record['mixed']):
        leaves=[0]*512
        leaves[256]=oracle.descriptor(int(9*n%2==0) if oracle.mode(profile,cpu) else 0,
                                      readonly=cpu!=intervals[-1]['id'])
        item.update(old_memory_after=record['cells'][cpu]['memory_after'],
                    new_memory_before=record['new_cells'][cpu]['memory_before'],
                    new_memory_after=record['new_cells'][cpu]['memory_after'],
                    current_el=4,sctlr=0x30d01985,actlr=2,
                    tcr=16|1<<8|1<<10|3<<12|1<<23|2<<32,service_pc=oracle.PRIVATE+0xa0080,
                    lower_leaf=leaves,walks=(6+int(cpu==0)+9*n+m['interval_counts'][cpu]) if oracle.mode(profile,cpu) else 0,
                    retries=0,faults=0,vector=0,error=0,linear=0,rip=0,traces=[])
    return record


class MixedSemanticsTests(unittest.TestCase):
    def test_complete_reports(self):
        for n in range(1,5):
            for profile in (1,2):
                with self.subTest(n=n,profile=profile):
                    oracle.check(fixture(n,profile),n,profile)

    def test_corrupted_report_controls(self):
        original = fixture()
        cases = [
            ('identity remapped', lambda r:r['mixed'][0]['routes'].__setitem__(1,oracle.DATA+4096)),
            ('wrong mode', lambda r:r['mixed'][0].__setitem__('mode',1)),
            ('cross owner reply', lambda r:r['mixed'][0]['replies'][2].__setitem__(3,1)),
            ('cross mode reply', lambda r:r['mixed'][0]['replies'][2].__setitem__(8,1)),
            ('stale source', lambda r:r['mixed'][0]['replies'][2].__setitem__(5,1)),
            ('premature code reuse', lambda r:r['source'].__setitem__('reuse_blocked',0)),
            ('lost prefix', lambda r:r['faults'][0]['frame']['x'].__setitem__(7,0)),
            ('store before fault', lambda r:r['cells'][0]['memory_before'].__setitem__(0,1)),
            ('wrong final CR2', lambda r:r['mixed'][0]['guest_after']['control'].__setitem__(1,0)),
            ('lost upper state', lambda r:r['mixed'][0]['guest_after']['gpr'].__setitem__(9,0)),
            ('identity walked tables', lambda r:r['mixed'][0].__setitem__('walks',1)),
            ('paged walk missing', lambda r:r['mixed'][1].__setitem__('walks',0)),
            ('native wrong GPA', lambda r:r['mixed'][0]['lower_leaf'].__setitem__(256,oracle.descriptor(1))),
            ('native all writable', lambda r:r['mixed'][2]['lower_leaf'].__setitem__(256,oracle.descriptor(0,False))),
            ('native fault fabricated', lambda r:r['new_cells'][0]['dirty_fault'].__setitem__('pc',oracle.CODE_NEXT+12)),
            ('wrong final store', lambda r:r['new_cells'][0]['captured']['x'].__setitem__(7,0)),
            ('source D attributed to off', lambda r:r['mixed_tables_after'].__setitem__(0x3810,0x67)),
            ('unexpected table write', lambda r:r['mixed_tables_after'].__setitem__(15,1)),
            ('stale published body', lambda r:r['mixed'][0]['new_code'].__setitem__(8,0xc7)),
            ('retired live body', lambda r:r['mixed'][0]['old_code'].__setitem__(0,0xe7)),
            ('lost owner publication', lambda r:r['trace'].remove((16<<8)|1)),
            ('duplicate publication', lambda r:r['trace'].insert(r['trace'].index(16<<8),16<<8)),
            ('unowned byte', lambda r:r['mixed_pages_after'].__setitem__(4095,1)),
        ]
        for name, mutate in cases:
            changed = copy.deepcopy(original)
            mutate(changed)
            with self.subTest(name=name), self.assertRaises((ValueError,AssertionError)):
                oracle.check(changed)


    def test_single_off_tables_have_no_ad(self):
        record=fixture(1,1)
        self.assertEqual(record['maintenance']['leaf_history'][0],oracle.DATA|7)
        self.assertEqual(record['mixed_tables_after'][0:8],list((0x2007).to_bytes(8,'little')))
        for at in (0,0x1000,0x2000,0x3800,0x3810):
            altered=copy.deepcopy(record)
            altered['mixed_tables_after'][at]|=32
            with self.subTest(at=at),self.assertRaises(ValueError):
                oracle.check(altered)

    def test_all_reply_words_are_bound(self):
        record=fixture(2,2)
        for cpu in range(2):
            for serial in range(3):
                for word in range(16):
                    altered=copy.deepcopy(record)
                    altered['mixed'][cpu]['replies'][serial][word]^=1
                    with self.subTest(cpu=cpu,serial=serial,word=word),self.assertRaises(ValueError):
                        oracle.check(altered)

    def test_literal_mov_effects(self):
        frame, _, memory = oracle.check_i4.seed(0)
        for reg in oracle.GPR_ROOTS:
            frame['x'][reg] &= 0xffffffff
        frame['x'][4], frame['x'][5] = 0xabcdef12, oracle.DATA + 128
        memory[128:132] = bytes.fromhex('78563412')
        old, after, fault = oracle.execute(frame, memory, oracle.CODE, oracle.PRIVATE+0x80, False)
        self.assertEqual(fault['x'][7], 0xabcdef12)
        self.assertEqual(fault['x'][4], 0xabcdef12)
        self.assertEqual(after[:4], [0x12, 0xef, 0xcd, 0xab])
        self.assertEqual(old['x'][4], 0x12345678)
        self.assertEqual(old['x'][7], 0xabcdef12)
        new, after, fault = oracle.execute(old, memory, oracle.CODE_NEXT, oracle.PRIVATE+0x80, True)
        self.assertEqual(after[:4], [0x80, 0, 0x10, 0])
        self.assertEqual(new['x'][7], 0x12345678)
        self.assertEqual(new['x'][4], 0x12345678)
        self.assertEqual(new['nzcv'], frame['nzcv'])
        self.assertEqual(new['q'], frame['q'])
        for reg in set(range(31)) - {4, 7, 16, 30}:
            self.assertEqual(new['x'][reg], frame['x'][reg])

    def test_identity_packets_are_distinct(self):
        for profile in (1, 2):
            for cpu in range(4):
                for serial in (1, 2, 3):
                    p = oracle.packet(19, cpu, profile, serial)
                    r = oracle.reply(19, cpu, profile, serial)
                    self.assertEqual(p[3], cpu)
                    self.assertEqual(p[8], (cpu+profile-1) % 2)
                    self.assertEqual(p[6], p[8]+1)
                    self.assertEqual(p[5], 2 if serial == 3 else 1)
                    self.assertEqual(r[:13], [0x49345642]+p[1:13])
                    self.assertEqual(b''.join(v.to_bytes(8, 'little') for v in r[13:])[:20],
                                     oracle.BODY_NEW if serial == 3 else oracle.BODY_OLD)
                    self.assertNotEqual(p, oracle.packet(19, cpu, 3-profile, serial))


if __name__ == '__main__':
    unittest.main()

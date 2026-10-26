import copy
import unittest
import i4v_controls_check as oracle
import i4v_pae_model as pae


def fixture(profile):
    capture=oracle.PRIVATE+0xa0080
    e=oracle.expectations(profile,123,capture)
    result={key:copy.deepcopy(e[key]) for key in
        ('initial','entered','final_frame','guest_before','guest_after','code_before','code_after',
         'data_before','data_after','tables_before','tables_after','source_before','source_after','visits',
         'pae','pae_tables_before','pae_tables_after')}
    result.update(case='controls32',profile=profile,rip=oracle.SOURCE,vm_nonce=123,capture_pc=capture,
                  service_pc=oracle.PRIVATE+0xa0180,status=1,detail=0,live=1,current_el=4,
                  tcr=0x2b5103510,ttbr1=0x40017000,sctlr=0x30d01985,actlr=2,
                  requests=e['state']['serial'],request=[0]*16,reply=[0]*16,
                  completed=e['state']['completed'],stage=e['stage'],operand=0,width=0,
                  controls=copy.deepcopy(e['state']),bank_before=e['bank'],
                  compiler_requests=copy.deepcopy(e['compile_requests']),
                  lower_leaf_before=e['lower_leaf_before'],lower_leaf_after=e['lower_leaf'])
    faults=[v for v in e['visits'] if v['faults']]
    for key,value in zip(('esr','far','elr','spsr'),faults[-1]['exception'] if faults else [0]*4):result[key]=value
    last=e['exception_record']
    result['recovery']={'capture_pc':capture,'guest_before':e['guest_before'],'guest_after':e['guest_after'],
        'control':[0x1000,0,1,0,0],'faults':0,'walks':0,'retries':0,'traces':[],
        'vector':last['vector'],'error':last['error'],'linear':last['linear'],
        'rip':last['rip'] if last['vector'] else 0,'tables_before':e['tables_before'],
        'tables_after':e['tables_after'],'memory_after':e['data_after'][:256],'bank_after':e['bank']}
    return result


def change(path,value):
    def mutate(record):
        target=record
        for key in path[:-1]:target=target[key]
        target[path[-1]]=value
    return mutate


class ControlSemanticsTests(unittest.TestCase):
    def test_pae_reload_is_distinct_from_eviction(self):
        memory = pae.tables(13)
        addresses, hidden, error = pae.load(memory, 0x1020)
        self.assertEqual(addresses, [0x1020, 0x1028, 0x1030, 0x1038])
        self.assertEqual((hidden, error), ([0x2001, 0, 0, 0], None))
        pae.qword(memory, 0x20, 0x4001)
        self.assertEqual(pae.walk(memory, hidden, oracle.DATA), (oracle.DATA, None))
        self.assertEqual(pae.qword(memory, 0x20), 0x4001)
        replacement = pae.load(memory, 0x1020)[1]
        self.assertEqual(pae.walk(memory, replacement, oracle.DATA), (oracle.DATA + 4096, None))
        before = (0x80010011, 0x1020, 0x620)
        self.assertTrue(pae.needs_reload(before, before, 3))
        self.assertFalse(pae.needs_reload(before, (0x80000011, 0x1020, 0x620), 0))
        self.assertTrue(pae.needs_reload(before, (0x80010011, 0x1020, 0x6a0), 4))
        self.assertFalse(pae.needs_reload(before, (0x10011, 0x1020, 0x620), 0))
        self.assertTrue(pae.needs_reload((0x10011, 0x1020, 0x620), before, 0))

    def test_pae_invalid_unused_slot_and_nonpresent_payload(self):
        addresses, values, error = pae.load(pae.tables(17), 0x1020)
        self.assertEqual((len(addresses), values[3], error), (4, 0x21, 13))
        memory = pae.tables(20)
        _, hidden, error = pae.load(memory, 0x1020)
        self.assertIsNone(error)
        snapshot = list(memory)
        self.assertEqual(pae.walk(memory, hidden, oracle.DATA), (None, 0))
        self.assertEqual(pae.walk(memory, hidden, oracle.DATA, True), (None, 2))
        self.assertEqual(memory, snapshot)

    def test_pae_program_prefixes_and_exact_read_counts(self):
        # visits, completed, context, walks, native faults, reloads, reads, A/D stores
        vectors=[(5,5,3,2,1,1,4,3), (10,10,5,9,3,2,18,6),
                 (9,9,4,8,3,2,16,6), (11,11,5,10,3,2,20,6),
                 (6,5,2,0,1,1,0,0), (10,9,3,9,3,2,18,4),
                 (10,9,3,9,3,2,18,4), (5,4,3,1,0,1,0,0),
                 (12,12,5,11,3,3,22,6)]
        for profile,wanted in enumerate(vectors,13):
            answer=oracle.model(profile,0x04040404);state=answer['pae']
            with self.subTest(profile=profile):
                self.assertEqual((len(answer['records']),answer['completed'],answer['generation'],
                    answer['walks'],sum(v['native_fault'] for v in answer['records']),
                    state['attempts'],state['walk_reads'],state['walk_writes']),wanted)
                self.assertEqual(state['reads'],4*state['attempts'])
                self.assertEqual(answer['tables'],oracle.tables(profile))
        for profile,second in ((14,6),(15,5),(16,7)):
            records=oracle.model(profile,0)['records']
            self.assertEqual(records[4]['eax_after'],0x11220000)
            self.assertEqual(records[second]['eax_after'],0x11220000)
            self.assertEqual(records[-1]['eax_after'],0x33440000)
        for profile in (18,19):
            answer=oracle.model(profile,0);records=answer['records']
            self.assertEqual((records[8]['vector'],records[8]['error'],records[8]['linear']),(13,0,0))
            self.assertEqual(records[8]['before'],records[8]['after'])
            self.assertEqual((records[-1]['eax_after'],records[-1]['vector']),(0x10011,0))
            self.assertEqual(answer['pae']['hidden'],{'entries':[0x2001,0,0,0],'generation':1})
            self.assertEqual([i for i,r in enumerate(records) if r['native_permission']],[6])
        np=oracle.model(20,0)['records'][-1]
        self.assertEqual((np['vector'],np['error'],np['linear'],np['after'][0]),
                         (14,0,oracle.SOURCE+16,0x80010011))
        aba=oracle.model(21,0)['records']
        self.assertEqual([aba[i]['eax_after'] for i in (4,7,11)],
                         [0x11220000,0x33440000,0x11220000])
        self.assertEqual(aba[4]['rip'],aba[11]['rip'])
        self.assertEqual((aba[4]['generation_before'],aba[11]['generation_before']),(3,5))

    def test_pae_corrupted_reports_are_rejected(self):
        cases=[
            (13,'root incorrectly page aligned',['pae','visits',3,'load_addresses',0],0x1000),
            (13,'missing unused slot read',['pae','reads'],3),
            (13,'invented root A bit',['pae_tables_after',0x20],0x21),
            (13,'legacy table used',['tables_after',0],0x27),
            (14,'INVLPG skipped',['pae','invlpg'],0),
            (14,'INVLPG reloaded root',['pae','visits',5,'after','generation'],2),
            (14,'wrong post-INVLPG route',['visits',6,'gpr_after',0],0x33440000),
            (15,'eviction skipped',['pae','evictions'],0),
            (15,'eviction reloaded root',['visits',5,'gpa'],oracle.DATA+4096),
            (16,'WP reloaded root',['pae','visits',6,'after','generation'],2),
            (16,'PGE did not reload',['pae','visits',9,'after','generation'],1),
            (17,'invalid unused slot ignored',['visits',5,'vector'],0),
            (17,'failed activation committed',['guest_after','control',0],0x80010011),
            (18,'failed reload cleared snapshot',['pae','hidden','entries',0],0),
            (18,'permission fault disguised as cold',['visits',6,'exception',0],(0x25<<26)|64|7),
            (19,'permission fault disguised as cold',['visits',6,'exception',0],(0x25<<26)|64|7),
            (13,'cold fault disguised as permission',['visits',4,'exception',0],(0x25<<26)|15),
            (18,'failed reload advanced hidden generation',['pae','hidden','generation'],2),
            (18,'failed reload published replacement',['pae','hidden','entries',0],0x4001),
            (18,'lost committed store',['data_after',0],0),
            (18,'handler falsely completed MOV',['completed'],10),
            (18,'handler discarded fault',['recovery','vector'],0),
            (19,'fault changed CR2',['guest_after','control',1],oracle.DATA),
            (19,'handler wrong final native route',['lower_leaf_after',256],oracle.descriptor(1)),
            (20,'nonpresent rejected during load',['pae','hidden','generation'],0),
            (20,'nonpresent followed directory',['pae','walk_reads'],1),
            (20,'postcommit fetch rollback',['guest_after','control',0],0x10011),
            (21,'ABA stale entry reused',['controls','stale_entry'],0),
            (21,'ABA stale reply rebound admitted',['controls','stale_reply'],1),
            (21,'PAE compiled as legacy',['visits',11,'request',8],1),
            (21,'hidden generation ABA',['pae','hidden','generation'],1),
        ]
        for profile,name,path,value in cases:
            report=fixture(profile);change(path,value)(report)
            with self.subTest(name=name),self.assertRaises(ValueError):oracle.check(report)

    def test_full_reports_for_every_program(self):
        for profile in range(22):
            with self.subTest(profile=profile):oracle.check(fixture(profile),profile)

    def test_corrupted_observation_controls(self):
        cases=[
            (0,'source alias missing initially',change(['lower_leaf_before',258],0)),
            (0,'source alias erased by invalidation',change(['lower_leaf_after',258],0)),
            (0,'source alias wrong physical page',change(['lower_leaf_before',258],oracle.descriptor(1,False))),
            (0,'source alias executable',change(['lower_leaf_after',258],oracle.descriptor(2,False)&~(3<<53))),
            (0,'stale entry admitted',change(['controls','stale_entry'],0)),
            (0,'serial-only rejection',change(['controls','stale_reply'],1)),
            (0,'rebound context',change(['controls','rebound_reply',6],5)),
            (0,'wrong actual source',change(['visits',15,'source'],0)),
            (0,'lost guest state',change(['guest_after','gpr',9],0)),
            (0,'native scratch damaged',change(['visits',15,'frame','x',20],0)),
            (0,'unowned data byte',change(['data_after',8191],0)),
            (0,'source changed',change(['source_after',4095],0)),
            (0,'table entry width',change(['tables_after',0x1404],32)),
            (0,'unexpected published slot',change(['code_after',3*32],1)),
            (1,'samevalue reload skipped',change(['controls','invalidations'],1)),
            (1,'fixture dropped warm route',change(['controls','warm_before_reload'],0)),
            (1,'wrong remapped result',change(['visits',5,'gpr_after',0],0x11220000)),
            (1,'old native route retained',change(['lower_leaf_after',256],oracle.descriptor(0))),
            (2,'faulting write committed',change(['data_after',0],255)),
            (2,'denied operation completed',change(['completed'],8)),
            (2,'wrong PF class',change(['visits',7,'error'],7)),
            (2,'wrong CR2',change(['guest_after','control',1],0)),
            (2,'advanced fault RIP',change(['guest_after','rip'],0x10201e)),
            (3,'illegal control committed',change(['guest_after','control',0],0x80010010)),
            (3,'illegal context committed',change(['controls','generation'],2)),
            (6,'CPL3 silently downgraded',change(['guest_after','segments',0,0],8)),
            (7,'nextfetch rollback',change(['guest_after','control',2],0x1000)),
            (7,'fetch fault compiled',change(['visits',6,'entries'],1)),
            (7,'legacy fetch I/D invented',change(['visits',6,'error'],16)),
            (8,'ignored bits erased',change(['guest_after','control',2],0x1000)),
            (9,'operand-size changed control write',change(['guest_after','control',0],0x10011)),
            (10,'LOCK accepted',change(['visits',3,'vector'],0)),
            (11,'reserved register GP',change(['visits',3,'vector'],13)),
            (12,'PGE epoch skipped',change(['controls','generation'],3)),
        ]
        for profile,name,mutate in cases:
            record=fixture(profile);mutate(record)
            with self.subTest(name=name),self.assertRaises(ValueError):oracle.check(record)

    def test_every_compile_reply_word_is_bound(self):
        for word in range(16):
            record=fixture(0);record['visits'][15]['reply'][word]^=1
            with self.subTest(word=word),self.assertRaises(ValueError):oracle.check(record)

    def test_literal_source_and_backedge(self):
        instructions=oracle.program(0)
        self.assertEqual(instructions[:4],[bytes.fromhex(x) for x in ('b811000100','8903','b811000180','0f22c0')])
        self.assertEqual(sum(map(len,instructions))+int.from_bytes(instructions[-1][1:],'little',signed=True),0)
        self.assertEqual(oracle.program(9)[1],bytes.fromhex('660f2200'))

    def test_mov_control_commit_fault_distinction(self):
        before=(0x10011,0x1000,0x600)
        for instruction,value,vector in [('0f22c0',0x80010010,13),('0f22c0',0x20010011,13),
                                         ('0f22e0',0xffffffff,13),('f00f22c0',0x80010011,6),
                                         ('0f22c8',0x80010011,6)]:
            self.assertEqual(oracle.control(bytes.fromhex(instruction),value,*before,0),(None,vector))
        self.assertEqual(oracle.control(bytes.fromhex('0f22c0'),0x80010011,*before,3),(None,13))
        self.assertEqual(oracle.control(bytes.fromhex('660f2200'),0x80010011,*before,0),((0x80010011,0x1000,0x600),None))
        self.assertEqual(oracle.control(bytes.fromhex('0f22d8'),0x1003,*before,0),((0x10011,0x1003,0x600),None))

    def test_identity_and_actual_legacy_entry_width(self):
        t=oracle.tables(0); before=list(t)
        self.assertEqual(oracle.translate(t,0x10011,0xdead0000,oracle.DATA,True),(oracle.DATA,None))
        self.assertEqual(t,before)
        self.assertEqual(oracle.translate(t,0x80010011,0x1003,oracle.DATA),(oracle.DATA,None))
        self.assertEqual(oracle.word(t,0),0x2027)
        self.assertEqual(oracle.word(t,0x1400),oracle.DATA|0x27)
        self.assertEqual(oracle.word(t,0x1404),0)
        self.assertEqual(oracle.translate(t,0x80010011,0x3000,oracle.DATA),(oracle.DATA+4096,None))

    def test_wp_and_precise_next_fetch_failure(self):
        t=oracle.tables(2)
        self.assertEqual(oracle.translate(t,0x80000011,0x1000,oracle.DATA,True),(oracle.DATA,None))
        self.assertEqual(oracle.word(t,0x1400),oracle.DATA|0x65)
        snapshot=copy.deepcopy(t)
        self.assertEqual(oracle.translate(t,0x80010011,0x1000,oracle.DATA,True),(None,3))
        self.assertEqual(t,snapshot)
        self.assertEqual(oracle.translate(t,0x80000011,0x1000,oracle.DATA,True,3),(None,7))
        t=oracle.tables(7)
        self.assertEqual(oracle.translate(t,0x80010011,0x3000,oracle.SOURCE+24),(None,0))
        self.assertEqual(oracle.word(t,0x2000),0x4027)
        self.assertEqual(oracle.word(t,0x3408),0)

    def test_complete_architectural_program_vectors(self):
        expected=[(17,17,5,12,5),(6,6,3,6,2),(8,7,3,8,2),(4,3,1,0,1),
                  (4,3,1,0,1),(4,3,1,0,1),(4,3,1,0,1),(7,6,3,3,1),
                  (5,5,3,2,1),(3,3,2,2,1),(4,3,1,0,1),(4,3,1,0,1),(9,9,4,10,3)]
        for profile,row in enumerate(expected):
            m=oracle.model(profile,0x04040404)
            self.assertEqual((len(m['records']),m['completed'],m['generation'],m['walks'],
                              sum(v['native_fault'] for v in m['records'])),row)
        reload=oracle.model(1,0)['records']
        self.assertEqual(reload[2]['eax_after'],0x11220000)
        self.assertEqual(reload[5]['eax_after'],0x33440000)
        self.assertEqual(reload[4]['before'][1],reload[4]['after'][1])
        self.assertEqual(reload[4]['generation_after'],reload[4]['generation_before']+1)
        wp=oracle.model(2,0)
        self.assertEqual(wp['memory'][:4],[0x11,0,1,0])
        self.assertEqual(wp['records'][-1]['next_rip'],0x10201c)
        self.assertEqual(wp['records'][-1]['eax_after'],0xffffffff)
        fetch=oracle.model(7,0)['records'][-1]
        self.assertEqual((fetch['next_rip'],fetch['vector'],fetch['error'],fetch['after'][1]),
                         (0x102017,14,0,0x3000))

    def test_actual_fex_body_vectors(self):
        expected={0x10011:'240280522400a072',0x80010011:'240280522400b072',0xffffffff:'04008012',
                  0x1003:'64008252',0x680:'04d08052'}
        for value,body in expected.items():self.assertEqual(oracle.body(oracle.immediate(value)).hex(),body)
        self.assertEqual(oracle.body(bytes.fromhex('8903')).hex(),'1f2003d5c4fc9f88')
        self.assertEqual(oracle.body(bytes.fromhex('8b03')).hex(),'c4fcdf881f2003d5')


if __name__=='__main__':unittest.main()

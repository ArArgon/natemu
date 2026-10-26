import copy
import unittest
import i4v_controls_check as oracle
import i4v_pae_model as pae
import i4v_modes_model as modes


def fixture(profile):
    capture=oracle.PRIVATE+0xa0080
    e=oracle.expectations(profile,123,capture)
    result={key:copy.deepcopy(e[key]) for key in
        ('initial','entered','final_frame','guest_before','guest_after','code_before','code_after',
         'data_before','data_after','tables_before','tables_after','source_before','source_after','visits',
         'pae','pae_tables_before','pae_tables_after','modes','mode_tables_before','mode_tables_after')}
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
    def test_modes_program_prefixes_and_lifecycle(self):
        # visits, completed, context, walks, native faults, descriptor/pointer reads,
        # terminal vector, error; exact values independently enumerated per program.
        vectors=[(15,15,8,11,3,3,0,0),(20,20,10,15,4,3,0,0),
                 (11,11,5,11,4,3,0,0),(10,9,5,2,0,0,13,0),
                 (4,3,2,0,0,0,13,0),(10,9,5,2,0,0,13,0),
                 (11,10,6,4,0,1,13,0),(2,1,1,0,0,0,13,0),
                 (10,9,6,3,0,1,6,0),(9,8,5,1,0,0,13,0x38),
                 (9,8,5,2,0,1,11,0x18),(9,8,5,2,0,1,13,0x18),
                 (11,10,6,6,1,1,14,0),(10,9,6,3,0,1,14,0),
                 (8,7,4,0,0,0,13,0),(9,8,5,2,0,1,13,0x10),
                 (11,10,6,7,1,3,13,0)]
        for profile,wanted in enumerate(vectors,22):
            answer=oracle.model(profile,0x04040404);last=answer['records'][-1]
            with self.subTest(profile=profile):
                self.assertEqual((len(answer['records']),answer['completed'],answer['generation'],
                    answer['walks'],sum(r['native_fault'] for r in answer['records']),
                    answer['modes']['reads'],last['vector'],last['error']),wanted)
                self.assertEqual(answer['tables'],oracle.tables(profile))
                self.assertEqual(answer['pae']['attempts'],int(profile==23))
        ordinary=oracle.model(22,0)['records']
        self.assertEqual([ordinary[i]['eax_after'] for i in (9,11,14)],
                         [0xa5a5a5a511220000,0x11220000,0x11220000])
        self.assertEqual((ordinary[7]['efer_after'],ordinary[7]['cs_after']),
                         (0x500,modes.CS32))
        self.assertEqual(ordinary[8]['cs_after'],modes.CS64)
        self.assertEqual(ordinary[10]['cs_after'],modes.CS32)
        self.assertEqual(ordinary[13]['efer_after'],0x100)
        pae_answer=oracle.model(23,0)
        self.assertEqual((pae_answer['pae']['reads'],pae_answer['pae']['walk_reads'],
                          pae_answer['pae']['walk_writes']),(4,8,3))
        self.assertEqual(pae_answer['pae']['hidden'],{'entries':[0x2001,0,0,0],'generation':1})
        aba=oracle.model(24,0)['records']
        self.assertEqual((aba[0]['rip'],aba[10]['rip']),(oracle.SOURCE,oracle.SOURCE))
        self.assertEqual((aba[0]['before'],aba[0]['efer_before']),
                         (aba[10]['before'],aba[10]['efer_before']))
        self.assertEqual((aba[0]['generation_before'],aba[10]['generation_before']),(1,5))

    def test_modes_decoding_seed_and_pointer_bytes(self):
        r=fixture(22)
        self.assertEqual(r['initial']['x'][7],0xc0000080)
        self.assertEqual(r['initial']['x'][5],0)
        self.assertEqual(r['initial']['x'][10],oracle.DATA+0x100)
        self.assertEqual(oracle.program(22)[8],bytes.fromhex('ea262010001800'))
        self.assertEqual(r['data_before'][0x100:0x10a],list(bytes.fromhex('2c201000000000000800')))
        self.assertEqual(oracle.body(bytes.fromhex('488b03')).hex(),'c4fcdfc81f2003d5')
        self.assertEqual([r['visits'][i]['request'][8] for i in (6,9,11,14)],[0,4,3,0])
        self.assertEqual(r['modes']['visits'][8]['descriptor_address'],oracle.DATA+0x218)
        self.assertEqual(r['modes']['visits'][10]['pointer_address'],oracle.DATA+0x100)
        self.assertEqual(r['modes']['visits'][10]['descriptor_address'],oracle.DATA+0x208)

    def test_invalid_long_default_precedes_nonpresent_descriptor(self):
        memory=oracle.data_pages(32)
        pae.qword(memory,0x218,0x00ef1b000000ffff)
        answer=modes.model(32,0x04040404,memory)
        fault=answer['records'][-1]
        self.assertEqual((fault['vector'],fault['error']),(13,0x18))
        self.assertEqual(fault['cs_after'],modes.CS32)
        self.assertEqual(fault['generation_before'],fault['generation_after'])
        self.assertEqual(fault['next_rip'],fault['rip'])
        self.assertEqual(answer['modes']['descriptor_reads'],1)
        self.assertEqual(oracle.model(32,0)['records'][-1]['vector'],11)
        self.assertEqual(oracle.model(33,0)['records'][-1]['vector'],13)

    def test_modes_corrupted_reports_are_rejected(self):
        cases=[
            (22,'PG activation selected long too early',['modes','visits',7,'mode_after'],4),
            (22,'LMA missing',['visits',7,'after',4],0x100),
            (22,'long source compiled32',['visits',9,'request',8],3),
            (22,'qword load truncated',['visits',9,'gpr_after',0],0x11220000),
            (22,'compat load retained upperhalf',['visits',11,'gpr_after',0],0xa5a5a5a511220000),
            (22,'far jump not invalidated',['controls','invalidations'],6),
            (22,'pointer read omitted',['modes','pointer_reads'],0),
            (22,'descriptor source fabricated',['modes','visits',8,'descriptor'],0),
            (22,'GDT byte changed',['data_after',0x208],0),
            (22,'LMA retained after PGoff',['guest_after','control',4],0x500),
            (22,'unused PAE loaded',['pae','attempts'],1),
            (23,'PAE state lost after switch',['pae','hidden','entries',0],0),
            (23,'PAE reloaded by IA32e activation',['pae','hidden','generation'],2),
            (24,'ABA old native entry admitted',['controls','stale_entry'],0),
            (24,'ABA old reply rebound admitted',['controls','stale_reply'],1),
            (24,'ABA same tuple wrongly reused',['visits',10,'request',6],1),
            (25,'active LME clear committed',['guest_after','control',4],0x400),
            (26,'PG without PAE committed',['guest_after','control',0],0x80010011),
            (27,'active PAE clear committed',['guest_after','control',3],0x600),
            (28,'long PGclear committed',['guest_after','control',0],0x10011),
            (29,'reserved EFER accepted',['visits',1,'vector'],0),
            (30,'immediate far accepted64',['visits',9,'vector'],0),
            (31,'descriptor read beyond GDT',['modes','descriptor_reads'],1),
            (31,'wrong selector error',['recovery','error'],0),
            (32,'nonpresent code became GP',['recovery','vector'],13),
            (33,'L=D accepted',['guest_after','segments',0,0],0x18),
            (34,'failed pointer read claimed',['modes','pointer_reads'],1),
            (34,'pointer PF wrong CR2',['guest_after','control',1],oracle.DATA),
            (34,'pointer fault committed CS',['guest_after','segments',0,0],8),
            (34,'pointer fault bytes fabricated',['modes','visits',10,'pointer_bytes',0],1),
            (35,'postcommit fetch rolled CS back',['guest_after','segments',0,0],8),
            (35,'postcommit fetch compiled',['visits',9,'entries'],1),
            (35,'source PTE not cleared',['mode_tables_after',0x3810],7),
            (36,'16bit TSS activation committed',['guest_after','control',0],0x80010011),
            (37,'data selector error lost',['recovery','error'],0),
            (38,'noncanonical target committed',['guest_after','rip'],0x800000000000),
            (38,'noncanonical target changed CR2',['guest_after','control',1],0x800000000000),
            (38,'target validation skipped descriptor',['modes','descriptor_reads'],1),
        ]
        for profile,name,path,value in cases:
            report=fixture(profile);change(path,value)(report)
            with self.subTest(name=name),self.assertRaises(ValueError):oracle.check(report)

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
        for profile in range(39):
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
